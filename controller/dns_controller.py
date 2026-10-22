"""
Stage 5: learning switch + UDP 5353 DNS rules + ARP responder for virtual IPs
         + heartbeat receiver (UDP 9999) + server health table.
Later stages add: query redirection, load balancing, recovery, logging.

Run:  ryu-manager controller/dns_controller.py
"""
import json
import time
from datetime import datetime

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp, arp

DNS_PORT = 5353
HB_PORT = 9999
HB_TIMEOUT = 6.0          # seconds without heartbeat -> server DOWN
CTRL_IP = '10.0.0.254'    # virtual controller IP (heartbeat destination)

# virtual IPs the controller answers ARP for
VIRTUAL_IPS = {
    '10.0.0.254': '00:00:00:00:00:fe',   # controller
    '10.0.0.100': '00:00:00:00:00:64',   # virtual resolver (used in Stage 6)
}

KNOWN_SERVERS = {'ns1': '10.0.0.21', 'ns2': '10.0.0.22', 'ns3': '10.0.0.23'}

PRIO_MISS = 0
PRIO_L2 = 1
PRIO_DNS_TO_CTRL = 5
PRIO_HB_TO_CTRL = 8
PRIO_DNS_FLOW = 10


def ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


class DnsController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}   # {dpid: {mac: port}}
        # health table: name -> {ip, alive, load, last_seen}
        self.servers = {
            n: {'ip': ip, 'alive': False, 'load': 0, 'last_seen': 0.0}
            for n, ip in KNOWN_SERVERS.items()
        }
        self.monitor_thread = hub.spawn(self._monitor)

    # ---- flow helper --------------------------------------------------
    def add_flow(self, dp, priority, match, actions, idle=0, hard=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                instructions=inst, idle_timeout=idle,
                                hard_timeout=hard)
        dp.send_msg(mod)

    # ---- switch connects ----------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]

        # priority 0: unknown traffic -> controller
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(), to_ctrl)

        # priority 5: every DNS query (UDP dst 5353) -> controller
        self.add_flow(dp, PRIO_DNS_TO_CTRL,
                      parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                      udp_dst=DNS_PORT), to_ctrl)

        # priority 8: heartbeats (UDP 9999 to the virtual controller IP)
        self.add_flow(dp, PRIO_HB_TO_CTRL,
                      parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                      ipv4_dst=CTRL_IP, udp_dst=HB_PORT),
                      to_ctrl)

        self.logger.info("[%s] Switch s%s connected, rules installed (miss, DNS, heartbeat)",
                         ts(), dp.id)

    # ---- ARP reply for virtual IPs ------------------------------------
    def send_arp_reply(self, dp, port, req, vmac):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(ethertype=0x0806,
                                         dst=req.src_mac, src=vmac))
        p.add_protocol(arp.arp(opcode=arp.ARP_REPLY,
                               src_mac=vmac, src_ip=req.dst_ip,
                               dst_mac=req.src_mac, dst_ip=req.src_ip))
        p.serialize()
        out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                  in_port=ofp.OFPP_CONTROLLER,
                                  actions=[parser.OFPActionOutput(port)],
                                  data=p.data)
        dp.send_msg(out)

    # ---- heartbeat handling -------------------------------------------
    def handle_heartbeat(self, src_ip, payload):
        try:
            msg = json.loads(payload.decode())
            if msg.get('type') != 'HB':
                return
            name = msg['server']
            load = int(msg.get('load', 0))
        except Exception:
            self.logger.warning("[%s] bad heartbeat from %s: %r",
                                ts(), src_ip, payload[:60])
            return

        # identify server by source IP (Mininet CLI turns 'ns1' args into IPs)
        name = {v['ip']: n for n, v in self.servers.items()}.get(src_ip, name)
        s = self.servers.get(name)
        if s is None:
            s = {'ip': src_ip, 'alive': False, 'load': 0, 'last_seen': 0.0}
            self.servers[name] = s
            self.logger.info("[%s] new server registered: %s (%s)",
                             ts(), name, src_ip)

        was_alive = s['alive']
        s['alive'] = True
        s['load'] = load
        s['last_seen'] = time.time()
        if not was_alive:
            self.logger.info("[%s] %s is UP (load=%s)", ts(), name, load)

    # ---- background monitor: failure detection + table print ----------
    def _monitor(self):
        ticks = 0
        while True:
            hub.sleep(1)
            ticks += 1
            now = time.time()
            for name, s in self.servers.items():
                if s['alive'] and now - s['last_seen'] > HB_TIMEOUT:
                    s['alive'] = False
                    self.logger.warning("[%s] %s marked DOWN (no heartbeat for %.0fs)",
                                        ts(), name, now - s['last_seen'])
            if ticks % 10 == 0:
                self._print_table()

    def _print_table(self):
        now = time.time()
        lines = ['--- server health table ---']
        for name, s in sorted(self.servers.items()):
            age = ('%.1fs ago' % (now - s['last_seen'])) if s['last_seen'] else 'never'
            lines.append('  %-4s %-10s %-4s load=%-4s last_seen=%s' %
                         (name, s['ip'], 'UP' if s['alive'] else 'DOWN',
                          s['load'], age))
        self.logger.info('\n'.join(lines))

    # ---- packets sent to controller -----------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == 0x88cc:      # ignore LLDP
            return

        # learn where the source MAC lives
        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port

        # ARP request for a virtual IP -> answer it, do not flood
        a = pkt.get_protocol(arp.arp)
        if a is not None and a.opcode == arp.ARP_REQUEST \
                and a.dst_ip in VIRTUAL_IPS:
            self.send_arp_reply(dp, in_port, a, VIRTUAL_IPS[a.dst_ip])
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        u = pkt.get_protocol(udp.udp)

        # heartbeat for the controller -> consume it, do not forward
        if ip is not None and u is not None \
                and ip.dst == CTRL_IP and u.dst_port == HB_PORT:
            last = pkt.protocols[-1] if pkt.protocols else b''
            payload = bytes(last) if isinstance(last, (bytes, bytearray)) else b''
            self.handle_heartbeat(ip.src, payload)
            return

        # DNS query?
        is_dns = ip is not None and u is not None and u.dst_port == DNS_PORT
        if is_dns:
            self.logger.info("[%s] DNS query on s%s: %s -> %s (in_port %s)",
                             ts(), dp.id, ip.src, ip.dst, in_port)

        # normal forwarding
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            if is_dns:
                match = parser.OFPMatch(in_port=in_port, eth_type=0x0800,
                                        ip_proto=17, ipv4_src=ip.src,
                                        ipv4_dst=ip.dst, udp_dst=DNS_PORT)
                self.add_flow(dp, PRIO_DNS_FLOW, match, actions, idle=30)
            else:
                match = parser.OFPMatch(in_port=in_port, eth_src=eth.src,
                                        eth_dst=eth.dst)
                self.add_flow(dp, PRIO_L2, match, actions, idle=60)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
