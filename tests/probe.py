#!/usr/bin/env python3
"""Send one DNS query every INTERVAL seconds for DURATION seconds and record the outcome.

Usage: probe.py ip:port name duration interval out.csv [timeout]
Prints a one-line summary and writes a CSV (t_s, ok, latency_ms, server).
"""
import csv
import os
import random
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import dns_proto as P  # noqa: E402


def summarize(rows):
    """rows = [(t_s, ok, latency_ms, server)] -> dict of metrics."""
    n = len(rows)
    lat = sorted(r[2] for r in rows if r[1] and r[2] != "")
    ok_t = [r[0] for r in rows if r[1]]
    end = rows[-1][0] if rows else 0
    gaps = [b - a for a, b in zip(ok_t, ok_t[1:])]
    gaps.append(end - ok_t[-1] if ok_t else end)
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None
    fails = [i for i, r in enumerate(rows) if not r[1]]
    if not fails:
        outage = 0.0
    elif fails[-1] == len(rows) - 1:
        outage = None          # still failing when the run ended (never recovered)
    else:                      # first failure until the final unbroken run of successes
        outage = round(rows[fails[-1] + 1][0] - rows[fails[0]][0], 2)
    return {"n": n, "success_pct": round(100.0 * len(ok_t) / n, 1) if n else 0.0,
            "mean_ms": round(sum(lat) / len(lat), 2) if lat else None,
            "p95_ms": round(p95, 2) if p95 is not None else None,
            "longest_gap_s": round(max(gaps), 2) if gaps else None,
            "outage_s": outage}


def main():
    host, port = sys.argv[1].rsplit(":", 1)
    port = int(port)
    name, duration, interval, out = sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    timeout = float(sys.argv[6]) if len(sys.argv) > 6 else 0.5
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    rows, first, start = [], None, time.time()
    while time.time() - start < duration:
        t0 = time.time()
        qid = random.randint(1, 65535)
        ok, server, lat = 0, "", ""
        sock.sendto(P.encode({"type": P.Q, "id": qid, "name": name}), (host, port))
        try:
            while True:
                m = P.decode(sock.recvfrom(P.MAX_DGRAM)[0])
                if m.get("id") == qid:
                    break
            if m.get("type") == P.RESP:
                ok, server, lat = 1, m.get("server", ""), round((time.time() - t0) * 1000, 2)
        except (socket.timeout, ValueError, OSError):
            pass
        rows.append((round(t0 - start, 3), ok, lat, server))
        if ok and first is None:
            first = server
            print("probe: first answer from", server, flush=True)
        rest = interval - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "ok", "latency_ms", "server"])
        w.writerows(rows)
    s = summarize(rows)
    print("probe summary: n=%(n)d success=%(success_pct)s%% mean=%(mean_ms)sms "
          "p95=%(p95_ms)sms longest_gap=%(longest_gap_s)ss" % s, flush=True)


if __name__ == "__main__":
    main()
