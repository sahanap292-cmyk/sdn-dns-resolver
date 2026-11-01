#!/usr/bin/env python3
"""UDP name server. One thread per request, mappings loaded from a JSON file.

Usage:
  python3 name_server.py --id ns1 --port 5353 --records records_ns.json \
      [--heartbeat 10.0.0.100:9999] [--delay 0]
"""
import argparse, json, logging, socket, threading, time
from concurrent.futures import ThreadPoolExecutor
import dns_proto as P


class NameServer:
    def __init__(self, sid, host, port, records_file, hb=None, delay=0.0):
        self.sid, self.hb, self.delay = sid, hb, delay
        self.records_file = records_file
        self.lock = threading.Lock()
        self.load()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = port
        self.pool = ThreadPoolExecutor(max_workers=16)
        self.stats = {"queries": 0, "answered": 0, "redirects": 0, "errors": 0}

    def load(self):
        with open(self.records_file) as f:
            cfg = json.load(f)
        with self.lock:
            self.ttl = int(cfg.get("ttl", 30))
            self.records = {P.normalize(k): v for k, v in cfg.get("records", {}).items()}
            # suffix -> "ip:port" of the server that is authoritative for it
            self.referrals = {P.normalize(k): v for k, v in cfg.get("referrals", {}).items()}
        logging.info("loaded %d records, %d referrals", len(self.records), len(self.referrals))

    def handle(self, data, addr):
        if self.delay:
            time.sleep(self.delay)  # simulate a slow/overloaded server
        qid = 0
        try:
            msg = P.decode(data)
            qid = msg.get("id", 0)
            if msg["type"] != P.Q:
                raise ValueError("expected QUERY")
            name = P.normalize(str(msg.get("name", "")))
            if not P.valid_name(name):
                return self.reply(addr, {"type": P.ERR, "id": qid, "code": "BAD_NAME",
                                         "msg": "invalid domain name"})
        except ValueError as e:
            self.stats["errors"] += 1
            return self.reply(addr, {"type": P.ERR, "id": qid, "code": "BAD_FORMAT", "msg": str(e)})

        self.stats["queries"] += 1
        with self.lock:
            ip = self.records.get(name)
            ref = next((t for s, t in self.referrals.items()
                        if name == s or name.endswith("." + s)), None)
        if ip:
            self.stats["answered"] += 1
            out = {"type": P.RESP, "id": qid, "name": name, "ip": ip, "ttl": self.ttl}
        elif ref:
            self.stats["redirects"] += 1
            out = {"type": P.REDIR, "id": qid, "name": name, "referral": ref}
        else:
            self.stats["errors"] += 1
            out = {"type": P.ERR, "id": qid, "code": "NXDOMAIN", "msg": f"{name} not found"}
        out["server"] = self.sid
        logging.info("%s %s -> %s", addr, name, out["type"])
        self.reply(addr, out)

    def reply(self, addr, msg):
        msg.setdefault("server", self.sid)
        self.sock.sendto(P.encode(msg), addr)

    def heartbeat_loop(self):
        host, port = self.hb.split(":")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        last = 0
        while True:
            now = self.stats["queries"]
            msg = {"type": P.HB, "server": self.sid, "port": self.port, "load": now - last}
            last = now
            s.sendto(P.encode(msg), (host, int(port)))
            time.sleep(2)

    def serve(self):
        if self.hb:
            threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        logging.info("%s listening on UDP %d", self.sid, self.port)
        while True:
            data, addr = self.sock.recvfrom(P.MAX_DGRAM)
            self.pool.submit(self.handle, data, addr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="ns1")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5353)
    ap.add_argument("--records", default="records_ns.json")
    ap.add_argument("--heartbeat", help="controller ip:port for heartbeats (optional)")
    ap.add_argument("--delay", type=float, default=0.0, help="artificial delay (s)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s")
    NameServer(a.id, a.host, a.port, a.records, a.heartbeat, a.delay).serve()
