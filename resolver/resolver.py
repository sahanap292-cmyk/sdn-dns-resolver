"""
resolver.py - recursive-style resolver with a TTL cache and two upstream-selection modes.

  --mode static : try the name servers in a fixed order, moving on after a timeout
                  (the "static baseline" for the evaluation)
  --mode sdn    : poll the SDN controller for server health; skip servers marked DOWN
                  and put degraded (lossy) servers last

Usage (inside the Mininet host r1):
    python3 resolver.py --mode sdn --port 5300 \
        --upstream ns1=10.0.0.11:5301 --upstream ns2=10.0.0.12:5301 \
        --upstream ns3=10.0.0.13:5301 --controller 10.0.0.254:9999

Control messages (used by run_eval.py):
    {"type":"stats"} -> counters      {"type":"flush"} -> clear cache + counters
"""
import argparse
import json
import socket
import threading
import time

from common import (CONTROLLER_PORT, DEGRADED_LOSS, RESOLVER_PORT, parse_hostport,
                    udp_request)


class TTLCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return (ip, remaining_ttl) or None if missing/expired."""
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            ip, expires = item
            remaining = expires - self.clock()
            if remaining <= 0:
                del self._d[key]
                return None
            return ip, remaining

    def put(self, key, ip, ttl):
        if ttl <= 0:
            return
        with self._lock:
            self._d[key] = (ip, self.clock() + ttl)

    def clear(self):
        with self._lock:
            self._d.clear()


class Resolver:
    def __init__(self, upstreams, mode="static", controller=None, timeout=0.5,
                 use_cache=True, poll_interval=0.5, clock=time.monotonic,
                 upstream_proto="m3"):
        self.upstreams = list(upstreams)     # [(name, ip, port), ...] in static order
        self.upstream_proto = upstream_proto  # m3 = our own JSON, m1 = QUERY/RESPONSE/ERROR
        self.mode = mode
        self.controller = controller         # (ip, port) or None
        self.timeout = timeout
        self.use_cache = use_cache
        self.poll_interval = poll_interval
        self.cache = TTLCache(clock)
        self._lock = threading.Lock()
        self._status = None                  # last health table from the controller
        self._poll_fail = 0
        self.reset_stats()

    # ---------- statistics ----------
    def reset_stats(self):
        with self._lock:
            self.stats = {"queries": 0, "cache_hits": 0, "cache_misses": 0,
                          "upstream_queries": 0, "upstream_timeouts": 0, "servfail": 0}

    def _bump(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    def stats_snapshot(self):
        with self._lock:
            s = dict(self.stats)
        s["hit_rate"] = round(s["cache_hits"] / s["queries"], 4) if s["queries"] else 0.0
        return s

    # ---------- SDN health polling ----------
    def poll_once(self):
        if not self.controller:
            return
        resp = udp_request(self.controller, {"type": "get_status"}, 0.3)
        if resp and resp.get("type") == "status":
            self._status = resp["servers"]
            self._poll_fail = 0
        else:
            self._poll_fail += 1
            if self._poll_fail >= 3:         # controller unreachable: fall back to static order
                self._status = None

    def start_poller(self):
        if self.mode != "sdn" or not self.controller:
            return
        self.poll_once()

        def loop():
            while True:
                time.sleep(self.poll_interval)
                self.poll_once()
        threading.Thread(target=loop, daemon=True).start()

    def upstream_order(self):
        st = self._status
        if self.mode != "sdn" or not st:
            return list(self.upstreams)
        ranked, suspect = [], []
        for idx, u in enumerate(self.upstreams):
            info = st.get(u[0])
            if info is None or info.get("up"):
                degraded = bool(info) and info.get("loss", 0.0) > DEGRADED_LOSS
                ranked.append((degraded, idx, u))
            else:
                suspect.append(u)            # controller says DOWN: only try as a last resort
        ranked.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in ranked] + suspect

    # ---------- upstream protocol adapter ----------
    def _ask(self, ip, port, name, qid):
        """Ask one upstream server. Returns a dict (rcode, ip, ttl, server) or None on timeout."""
        if self.upstream_proto != "m1":
            return udp_request((ip, port), {"type": "query", "name": name, "id": qid},
                               self.timeout)
        resp = udp_request((ip, port), {"type": "QUERY", "id": qid or 0, "name": name},
                           self.timeout)
        if resp is None:
            return None
        kind = resp.get("type")
        if kind == "RESPONSE":
            return {"rcode": "OK", "ip": resp.get("ip"), "ttl": resp.get("ttl", 0),
                    "server": resp.get("server")}
        if kind == "ERROR":
            return {"rcode": resp.get("code", "SERVFAIL"), "ip": None, "ttl": 0,
                    "server": resp.get("server")}
        return {"rcode": "SERVFAIL", "ip": None, "ttl": 0, "server": resp.get("server")}

    @staticmethod
    def _to_m1(r):
        """Convert our answer into the QUERY/RESPONSE/ERROR format used by the UDP client."""
        if r.get("rcode") == "OK":
            return {"type": "RESPONSE", "id": r.get("id"), "name": r.get("name"),
                    "ip": r.get("ip"), "ttl": r.get("ttl"),
                    "server": r.get("source") or "resolver"}
        return {"type": "ERROR", "id": r.get("id"), "code": r.get("rcode"),
                "msg": "%s: %s" % (r.get("name"), r.get("rcode")), "server": "resolver"}

    # ---------- resolution ----------
    def resolve(self, qname, qid=None):
        name = (qname or "").lower().rstrip(".")
        self._bump("queries")
        if self.use_cache:
            hit = self.cache.get(name)
            if hit:
                self._bump("cache_hits")
                return {"type": "answer", "id": qid, "name": name, "rcode": "OK",
                        "ip": hit[0], "ttl": round(hit[1], 2), "source": "cache"}
        self._bump("cache_misses")
        for uname, uip, uport in self.upstream_order():
            self._bump("upstream_queries")
            resp = self._ask(uip, uport, name, qid)
            if resp is None:
                self._bump("upstream_timeouts")
                continue
            if resp.get("rcode") == "OK" and self.use_cache:
                self.cache.put(name, resp["ip"], resp.get("ttl", 0))
            return {"type": "answer", "id": qid, "name": name,
                    "rcode": resp.get("rcode", "SERVFAIL"), "ip": resp.get("ip"),
                    "ttl": resp.get("ttl", 0), "source": resp.get("server") or uname}
        self._bump("servfail")
        return {"type": "answer", "id": qid, "name": name, "rcode": "SERVFAIL",
                "ip": None, "ttl": 0, "source": None}

    # ---------- UDP server ----------
    def _handle(self, sock, data, addr):
        try:
            msg = json.loads(data.decode())
        except ValueError:
            return
        kind = msg.get("type")
        if kind in ("query", "QUERY"):
            reply = self.resolve(msg.get("name"), msg.get("id"))
            if kind == "QUERY":
                reply = self._to_m1(reply)
        elif kind == "stats":
            reply = {"type": "stats", **self.stats_snapshot()}
        elif kind == "flush":
            self.cache.clear()
            self.reset_stats()
            reply = {"type": "ok"}
        else:
            return
        try:
            sock.sendto(json.dumps(reply).encode(), addr)
        except OSError:
            pass

    def serve_forever(self, bind_ip="0.0.0.0", port=RESOLVER_PORT):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_ip, port))
        while True:
            data, addr = sock.recvfrom(4096)
            threading.Thread(target=self._handle, args=(sock, data, addr),
                             daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Caching resolver (static or SDN-aware)")
    ap.add_argument("--mode", choices=["static", "sdn"], default="static")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=RESOLVER_PORT)
    ap.add_argument("--upstream", action="append", required=True,
                    help="name=ip:port (repeat; order matters for static mode)")
    ap.add_argument("--controller", help="ip:port of the SDN controller health service")
    ap.add_argument("--timeout", type=float, default=0.5, help="per-upstream timeout (s)")
    ap.add_argument("--poll", type=float, default=0.5, help="SDN status poll interval (s)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--upstream-proto", choices=["m3", "m1"], default="m3",
                    help="message format spoken to the upstream servers")
    a = ap.parse_args()

    ups = []
    for item in a.upstream:
        name, _, hp = item.partition("=")
        ip, port = parse_hostport(hp)
        ups.append((name, ip, port))
    ctrl = parse_hostport(a.controller, CONTROLLER_PORT) if a.controller else None

    r = Resolver(ups, a.mode, ctrl, a.timeout, not a.no_cache, a.poll,
                 upstream_proto=a.upstream_proto)
    r.start_poller()
    print(f"[resolver] mode={a.mode} cache={'off' if a.no_cache else 'on'} "
          f"upstreams={[u[0] for u in ups]}", flush=True)
    r.serve_forever(a.bind, a.port)


if __name__ == "__main__":
    main()
