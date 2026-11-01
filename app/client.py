#!/usr/bin/env python3
"""UDP resolver client with timeout, retry, server failover and redirect following.

Usage:
  python3 client.py -s 10.0.0.2:5300 [-s 10.0.0.3:5300] www.example.com another.org
  python3 client.py -s 10.0.0.2:5300            # interactive prompt
"""
import argparse, random, socket, time
import dns_proto as P


def resolve(name, servers, timeout=1.0, retries=3, max_redirects=3):
    """Return (message, rtt_seconds). Tries each server in order; retries on timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    queue, hops, t0 = list(servers), 0, time.time()
    while queue:
        host, port = queue.pop(0)
        qid = random.randint(1, 65535)
        pkt = P.encode({"type": P.Q, "id": qid, "name": name})
        for attempt in range(1, retries + 1):
            sock.sendto(pkt, (host, port))
            try:
                while True:  # skip stale/duplicate replies with the wrong id
                    data, _ = sock.recvfrom(P.MAX_DGRAM)
                    msg = P.decode(data)
                    if msg.get("id") == qid:
                        break
            except socket.timeout:
                print(f"  timeout {host}:{port} (attempt {attempt}/{retries})")
                continue
            except ValueError:
                continue
            if msg["type"] == P.REDIR and hops < max_redirects:
                rh, rp = msg["referral"].split(":")
                print(f"  redirected by {msg.get('server')} -> {msg['referral']}")
                queue.insert(0, (rh, int(rp)))
                hops += 1
                break
            return msg, time.time() - t0
        else:
            print(f"  server {host}:{port} unresponsive, failing over")
    return {"type": P.ERR, "code": "TIMEOUT", "msg": "no server answered"}, time.time() - t0


def show(name, msg, rtt):
    if msg["type"] == P.RESP:
        print(f"{name} -> {msg['ip']} (ttl {msg['ttl']}s, from {msg['server']}, {rtt*1000:.1f} ms)")
    else:
        print(f"{name} -> {msg['type']} {msg.get('code')}: {msg.get('msg')} ({rtt*1000:.1f} ms)")


def parse(s):
    h, p = s.split(":")
    return h, int(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--server", action="append", required=True, help="ip:port (repeatable)")
    ap.add_argument("-t", "--timeout", type=float, default=1.0)
    ap.add_argument("-r", "--retries", type=int, default=3)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    servers = [parse(s) for s in a.server]
    names = a.names or iter(lambda: input("name> ").strip(), "quit")
    for n in names:
        if n:
            m, rtt = resolve(n, servers, a.timeout, a.retries)
            show(n, m, rtt)
