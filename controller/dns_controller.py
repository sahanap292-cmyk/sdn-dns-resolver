"""
Stage 7: SDN DNS controller (OpenFlow 1.3)
  - learning switch, UDP 5353 DNS rules, ARP for virtual IPs
  - heartbeat receiver (UDP 9999), health table, failure detection
  - query redirection (virtual resolver 10.0.0.100) with IP/MAC rewrite
  - load balancing: least-loaded (default) or round-robin
  - recovery logging, flood detection (flow stats) and client blocking
  - static baseline mode for the performance comparison

Run:
  ryu-manager controller/dns_controller.py                    (SDN mode)
  POLICY=round_robin ryu-manager controller/dns_controller.py
  MODE=static ryu-manager controller/dns_controller.py        (baseline)
"""
import json
import os
import time
from datetime import datetime

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp, arp

MODE = os.environ.get('MODE', 'sdn').lower()                 # sdn | static
POLICY = os.environ.get('POLICY', 'least_loaded').lower()    # least_loaded | round_robin
FLOOD_RATE = float(os.environ.get('FLOOD_RATE', '200'))      # queries/s per client
BLOCK_SECS = int(os.environ.get('BLOCK_SECS', '30'))
STATS_INTERVAL = 2                                           # seconds

DNS_PORT = 5353
HB_PORT = 9999
HB_TIMEOUT = 6.0                 # seconds without heartbeat -> DOWN
CTRL_IP = '10.0.0.254'           # virtual controller IP (heartbeats)
VIP_RESOLVER = '10.0.0.100'      # virtual resolver IP (clients query this)
VIP_MAC = '00:00:00:00:00:64'
EVENT_LOG = 'controller_events.log'

VIRTUAL_IPS = {
    CTRL_IP: '00:00:00:00:00:fe',
    VIP_RESOLVER: VIP_MAC,
}

# name -> (ip, mac)
KNOWN_SERVERS = {
    'ns1': ('10.0.0.21', '00:00:00:00:00:15'),
    'ns2': ('10.0.0.22', '00:00:00:00:00:16'),
    'ns3': ('10.0.0.23', '00:00:00:00:00:17'),
}
COOKIES = {'ns1': 1, 'ns2': 2, 'ns3': 3}   # tag redirect rules per server

PRIO_MISS = 0
PRIO_L2 = 1
PRIO_DNS_TO_CTRL = 5
PRIO_HB_TO_CTRL = 8
PRIO_DNS_FLOW = 10
PRIO_REDIRECT = 20
PRIO_BLOCK = 30
REDIRECT_IDLE = 10               # seconds


def ts():
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def static_ports():
    """Fixed topology: s1(h1-h4 | s2), s2(r1 | s1 | s3), s3(ns1-3 | s2)."""
    H = ['00:00:00:00:00:%02x' % i for i in range(1, 5)]
    R1 = '00:00:00:00:00:0a'
    NS = [KNOWN_SERVERS[n][1] for n in ('ns1', 'ns2', 'ns3')]
    t = {1: {}, 2: {}, 3: {}}
    for i, m in enumerate(H):
        t[1][m] = i + 1
    t[1][R1] = 5
    for m in NS:
        t[1][m] = 5
    t[2][R1] = 1
    for m in H:
        t[2][m] = 2
    for m in NS:
        t[2][m] = 3
    for i, m in enumerate(NS):
        t[3][m] = i + 1
    for m in H + [R1]:
        t[3][m] = 4
    return t


class DnsController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = static_ports()    # {dpid: {mac: port}}
        self.datapaths = {}                  # {dpid: datapath}
        self.rr = 0                          # rotation counter
        self.servers = {
            n: {'ip': ip, 'mac': mac, 'alive': False, 'load': 0,
                'last_seen': 0.0, 'down_since': None}
            for n, (ip, mac) in KNOWN_SERVERS.items()
        }
        self.prev_counts = {}                # {client_ip: packets} (flood detection)
        self.last_stats_time = None
        self.blocked = {}                    # {client_ip: unblock_time}
        self.event('Controller started: MODE=%s POLICY=%s FLOOD_RATE=%s/s BLOCK_SECS=%s'
                   % (MODE, POLICY, FLOOD_RATE, BLOCK_SECS))
        self.monitor_thread = hub.spawn(self._monitor)

    # ---- logging: console + file (Member 3 uses the file) -------------
    def event(self, text):
        line = '[%s] %s' % (ts(), text)
        self.logger.info(line)
        try:
            with open(EVENT_LOG, 'a') as f:
                f.write(line + '\n')
        except Exception:
            pass

    # ---- flow helpers -------------------------------------------------
    def add_flow(self, dp, priority, match, actions, idle=0, hard=0, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # empty action list = drop
        inst = ([parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
                if actions else [])
        mod = parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle, hard_timeout=hard)
        dp.send_msg(mod)

    def delete_by_cookie(self, dp, cookie):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        mod = parser.OFPFlowMod(datapath=dp, cookie=cookie,
                                cookie_mask=0xffffffffffffffff,
                                table_id=ofp.OFPTT_ALL,
                                command=ofp.OFPFC_DELETE,
                                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                match=parser.OFPMatch())
        dp.send_msg(mod)

    def remove_server_rules(self, name):
        cookie = COOKIES.get(name, 99)
        for dp in list(self.datapaths.values()):
            self.delete_by_cookie(dp, cookie)
        self.event('%s DOWN -> deleted its redirect rules on %d switches; '
                   'new queries go to healthy servers'
                   % (name, len(self.datapaths)))

    # ---- switch connects ----------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, PRIO_MISS, parser.OFPMatch(), to_ctrl)
        self.add_flow(dp, PRIO_DNS_TO_CTRL,
                      parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                      udp_dst=DNS_PORT), to_ctrl)
        self.add_flow(dp, PRIO_HB_TO_CTRL,
                      parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                      ipv4_dst=CTRL_IP, udp_dst=HB_PORT),
                      to_ctrl)
        self.event('Switch s%s connected, rules installed (miss, DNS, heartbeat)'
                   % dp.id)

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
            s = {'ip': src_ip, 'mac': None, 'alive': False, 'load': 0,
                 'last_seen': 0.0, 'down_since': None}
            self.servers[name] = s
            self.event('new server registered: %s (%s)' % (name, src_ip))

        was_alive = s['alive']
        s['alive'] = True
        s['load'] = load
        s['last_seen'] = time.time()
        if not was_alive:
            if s.get('down_since'):
                self.event('%s RECOVERED after %.1fs down (load=%s) -> back in rotation'
                           % (name, time.time() - s['down_since'], load))
                s['down_since'] = None
            else:
                self.event('%s is UP (load=%s)' % (name, load))

    # ---- background monitor -------------------------------------------
    def _monitor(self):
        ticks = 0
        while True:
            hub.sleep(1)
            ticks += 1
            now = time.time()

            # failure detection
            for name, s in list(self.servers.items()):
                if s['alive'] and now - s['last_seen'] > HB_TIMEOUT:
                    s['alive'] = False
                    s['down_since'] = now
                    if MODE == 'static':
                        self.event('%s marked DOWN (no heartbeat for %.0fs) '
                                   '[static mode: no action taken]'
                                   % (name, now - s['last_seen']))
                    else:
                        self.event('%s marked DOWN (no heartbeat for %.0fs)'
                                   % (name, now - s['last_seen']))
                        self.remove_server_rules(name)

            # end of a client block
            for ip, until in list(self.blocked.items()):
                if now > until:
                    del self.blocked[ip]
                    self.event('UNBLOCKED %s (block time over)' % ip)

            # ask the client-side switch for flow statistics
            if MODE != 'static' and ticks % STATS_INTERVAL == 0:
                dp = self.datapaths.get(1)
                if dp is not None:
                    dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))

            if ticks % 10 == 0:
                self._print_table()

    def _print_table(self):
        now = time.time()
        lines = ['--- server health table (MODE=%s POLICY=%s) ---' % (MODE, POLICY)]
        for name, s in sorted(self.servers.items()):
            age = ('%.1fs ago' % (now - s['last_seen'])) if s['last_seen'] else 'never'
            lines.append('  %-4s %-10s %-4s load=%-4s last_seen=%s' %
                         (name, s['ip'], 'UP' if s['alive'] else 'DOWN',
                          s['load'], age))
        if self.blocked:
            lines.append('  blocked clients: %s' % ', '.join(sorted(self.blocked)))
        self.logger.info('\n'.join(lines))

    # ---- flow statistics -> flood detection ---------------------------
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats(self, ev):
        if MODE == 'static':
            return
        dp = ev.msg.datapath
        if dp.id != 1:                       # clients are attached to s1
            return
        now = time.time()
        dt = (now - self.last_stats_time) if self.last_stats_time else None
        self.last_stats_time = now

        counts = {}
        for st in ev.msg.body:
            if st.priority != PRIO_REDIRECT:
                continue
            src = st.match.get('ipv4_src')
            dst = st.match.get('ipv4_dst')
            if dst != VIP_RESOLVER or src is None:
                continue                     # skip reverse rules
            counts[src] = counts.get(src, 0) + st.packet_count

        if dt and dt > 0.5:
            for src, c in counts.items():
                prev = self.prev_counts.get(src, 0)
                delta = c - prev if c >= prev else c   # rule was recreated
                rate = delta / dt
                if rate > FLOOD_RATE and src not in self.blocked:
                    self.block_client(dp, src, rate)
        self.prev_counts = counts

    def block_client(self, dp, src, rate):
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                ipv4_src=src, udp_dst=DNS_PORT)
        self.add_flow(dp, PRIO_BLOCK, match, [], hard=BLOCK_SECS)   # drop
        self.blocked[src] = time.time() + BLOCK_SECS
        self.event('FLOOD DETECTED: %s sent %.0f queries/s (limit %.0f) '
                   '-> BLOCKED for %ds' % (src, rate, FLOOD_RATE, BLOCK_SECS))

    # ---- server selection ---------------------------------------------
    def pick_server(self, client_ip):
        if MODE == 'static':
            # baseline: fixed client -> server mapping, health is ignored
            names = sorted(self.servers)
            idx = (int(client_ip.split('.')[-1]) - 1) % len(names)
            name = names[idx]
            return name, self.servers[name]

        alive = [n for n in sorted(self.servers)
                 if self.servers[n]['alive'] and self.servers[n].get('mac')]
        if not alive:
            return None
        if POLICY == 'round_robin':
            cands = alive
        else:                                # least_loaded, ties rotate
            lowest = min(self.servers[n]['load'] for n in alive)
            cands = [n for n in alive if self.servers[n]['load'] == lowest]
        name = cands[self.rr % len(cands)]
        self.rr += 1
        return name, self.servers[name]

    # ---- redirect a query sent to the virtual resolver IP -------------
    def redirect_query(self, dp, msg, in_port, eth, ip):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        choice = self.pick_server(ip.src)
        if choice is None:
            self.event('NO HEALTHY SERVER: dropped query %s -> %s'
                       % (ip.src, VIP_RESOLVER))
            return
        name, s = choice
        table = self.mac_to_port.setdefault(dp.id, {})
        out_port = table.get(s['mac'], ofp.OFPP_FLOOD)
        cookie = COOKIES.get(name, 99)

        # forward rule: client -> VIP  becomes  client -> chosen server
        fwd_match = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                    ipv4_src=ip.src, ipv4_dst=VIP_RESOLVER,
                                    udp_dst=DNS_PORT)
        fwd_actions = [parser.OFPActionSetField(ipv4_dst=s['ip']),
                       parser.OFPActionSetField(eth_dst=s['mac']),
                       parser.OFPActionOutput(out_port)]
        self.add_flow(dp, PRIO_REDIRECT, fwd_match, fwd_actions,
                      idle=REDIRECT_IDLE, cookie=cookie)

        # reverse rule: server -> client  becomes  VIP -> client
        rev_match = parser.OFPMatch(eth_type=0x0800, ip_proto=17,
                                    ipv4_src=s['ip'], ipv4_dst=ip.src,
                                    udp_src=DNS_PORT)
        rev_actions = [parser.OFPActionSetField(ipv4_src=VIP_RESOLVER),
                       parser.OFPActionSetField(eth_src=VIP_MAC),
                       parser.OFPActionOutput(in_port)]
        self.add_flow(dp, PRIO_REDIRECT, rev_match, rev_actions,
                      idle=REDIRECT_IDLE, cookie=cookie)

        self.event('REDIRECT on s%s: %s -> %s  =>  %s (%s), load=%s'
                   % (dp.id, ip.src, VIP_RESOLVER, name, s['ip'], s['load']))

        # forward the packet that triggered this decision
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=fwd_actions,
                                  data=data)
        dp.send_msg(out)

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

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port

        # ARP for a virtual IP -> answer it
        a = pkt.get_protocol(arp.arp)
        if a is not None and a.opcode == arp.ARP_REQUEST \
                and a.dst_ip in VIRTUAL_IPS:
            self.send_arp_reply(dp, in_port, a, VIRTUAL_IPS[a.dst_ip])
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        u = pkt.get_protocol(udp.udp)

        # heartbeat for the controller
        if ip is not None and u is not None \
                and ip.dst == CTRL_IP and u.dst_port == HB_PORT:
            last = pkt.protocols[-1] if pkt.protocols else b''
            payload = bytes(last) if isinstance(last, (bytes, bytearray)) else b''
            self.handle_heartbeat(ip.src, payload)
            return

        # query to the virtual resolver IP -> redirect
        if ip is not None and u is not None \
                and ip.dst == VIP_RESOLVER and u.dst_port == DNS_PORT:
            self.redirect_query(dp, msg, in_port, eth, ip)
            return

        # ordinary DNS query between real hosts
        is_dns = ip is not None and u is not None and u.dst_port == DNS_PORT
        if is_dns:
            self.logger.info("[%s] DNS query on s%s: %s -> %s (in_port %s)",
                             ts(), dp.id, ip.src, ip.dst, in_port)

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
