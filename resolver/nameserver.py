"""
nameserver.py - a tiny authoritative name server that also talks to the SDN controller.

Usage (inside a Mininet host):
    python3 nameserver.py --name ns1 --port 5301 --controller 10.0.0.254:9999

It answers {"type":"query","name":...} over UDP and, if --controller is given,
sends a heartbeat every HEARTBEAT_INTERVAL seconds plus 'started'/'stopping' events.
"""
import argparse
import json
import signal
import socket
import threading
import time

from common import (HEARTBEAT_INTERVAL, NS_PORT, ZONE, parse_hostport, udp_send)


class NameServer:
    def __init__(self, name, bind_ip="0.0.0.0", port=NS_PORT, controller=None,
                 zone=None, delay=0.0):
        self.name = name
        self.bind_ip = bind_ip
        self.port = port
        self.controller = controller       # (ip, port) or None
        self.zone = dict(zone if zone is not None else ZONE)
        self.delay = delay                 # artificial processing delay (seconds)
        self.served = 0
        self._stop = threading.Event()
        self._sock = None

    def lookup(self, qname):
        key = (qname or "").lower().rstrip(".")
        if key in self.zone:
            ip, ttl = self.zone[key]
            return {"rcode": "OK", "ip": ip, "ttl": ttl}
        return {"rcode": "NXDOMAIN", "ip": None, "ttl": 0}

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_ip, self.port))
        self.port = self._sock.getsockname()[1]      # useful when port 0 was requested
        self._sock.settimeout(0.2)
        threading.Thread(target=self._serve, daemon=True).start()
        if self.controller:
            udp_send(self.controller, {"type": "event", "name": self.name, "event": "started"})
            threading.Thread(target=self._heartbeats, daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            if msg.get("type") != "query":
                continue
            if self.delay:
                time.sleep(self.delay)
            resp = self.lookup(msg.get("name"))
            resp.update(type="answer", id=msg.get("id"), name=msg.get("name"),
                        server=self.name)
            self.served += 1
            try:
                self._sock.sendto(json.dumps(resp).encode(), addr)
            except OSError:
                break

    def _heartbeats(self):
        seq = 0
        while True:
            udp_send(self.controller, {"type": "heartbeat", "name": self.name,
                                       "port": self.port, "seq": seq,
                                       "load": self.served})
            seq += 1
            if self._stop.wait(HEARTBEAT_INTERVAL):
                break

    def stop(self, graceful=True):
        if graceful and self.controller:
            udp_send(self.controller, {"type": "event", "name": self.name, "event": "stopping"})
        self._stop.set()
        if self._sock:
            self._sock.close()


def main():
    ap = argparse.ArgumentParser(description="Tiny name server with SDN heartbeats")
    ap.add_argument("--name", required=True)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=NS_PORT)
    ap.add_argument("--controller", help="ip:port of the SDN controller health service")
    ap.add_argument("--delay", type=float, default=0.0, help="artificial delay per query (s)")
    a = ap.parse_args()

    ctrl = parse_hostport(a.controller) if a.controller else None
    ns = NameServer(a.name, a.bind, a.port, ctrl, delay=a.delay)
    ns.start()
    signal.signal(signal.SIGTERM, lambda *_: ns.stop(graceful=True))
    signal.signal(signal.SIGINT, lambda *_: ns.stop(graceful=True))
    print(f"[{a.name}] serving on {a.bind}:{ns.port}, controller={ctrl}", flush=True)
    while not ns._stop.is_set():
        time.sleep(0.2)


if __name__ == "__main__":
    main()
