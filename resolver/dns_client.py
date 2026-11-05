"""
dns_client.py - sends queries to the resolver and prints one JSON list of results.

Each row: {i, t_send, name, ok, rcode, ip, source, latency_ms}
Examples:
    python3 dns_client.py --resolver 10.0.0.100 --names www.example.sdn --count 5
    python3 dns_client.py --resolver 10.0.0.100 --names a.sdn,b.sdn --duration 10 --interval 0.1
"""
import argparse
import json
import random
import time

from common import RESOLVER_PORT, udp_request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolver", required=True)
    ap.add_argument("--port", type=int, default=RESOLVER_PORT)
    ap.add_argument("--names", required=True, help="comma separated")
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--duration", type=float, default=0, help="seconds; stops at count or duration")
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--random", action="store_true", help="pick names randomly instead of in order")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if not a.count and not a.duration:
        a.count = 1

    names = a.names.split(",")
    rng = random.Random(a.seed)
    rows, i, start = [], 0, time.time()
    while True:
        if a.count and i >= a.count:
            break
        if a.duration and time.time() - start >= a.duration:
            break
        name = rng.choice(names) if a.random else names[i % len(names)]
        t_send = time.time()
        t0 = time.perf_counter()
        resp = udp_request((a.resolver, a.port), {"type": "query", "name": name, "id": i},
                           a.timeout)
        elapsed = time.perf_counter() - t0
        rows.append({"i": i, "t_send": t_send, "name": name,
                     "ok": bool(resp and resp.get("rcode") == "OK"),
                     "rcode": resp.get("rcode") if resp else "TIMEOUT",
                     "ip": resp.get("ip") if resp else None,
                     "source": resp.get("source") if resp else None,
                     "latency_ms": round(elapsed * 1000, 2)})
        i += 1
        rest = a.interval - elapsed
        if rest > 0:
            time.sleep(rest)
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
