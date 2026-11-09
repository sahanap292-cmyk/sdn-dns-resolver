#!/usr/bin/env python3
"""Print a markdown table of every results/probe_*.csv.   Usage: compare.py [results_folder]"""
import csv
import glob
import os
import sys

from probe import summarize

folder = sys.argv[1] if len(sys.argv) > 1 else "results"
print("| Run | Queries | Success % | Mean ms | p95 ms | Longest gap (s) | Outage until sustained recovery (s) |")
print("|---|---|---|---|---|---|---|")
for path in sorted(glob.glob(os.path.join(folder, "probe_*.csv"))):
    rows = []
    for r in csv.DictReader(open(path)):
        rows.append((float(r["t_s"]), int(r["ok"]),
                     float(r["latency_ms"]) if r["latency_ms"] else "", r["server"]))
    s = summarize(rows)
    name = os.path.basename(path)[6:-4]
    outage = "never recovered" if s["outage_s"] is None else s["outage_s"]
    print("| %s | %d | %s | %s | %s | %s | %s |" % (name, s["n"], s["success_pct"], s["mean_ms"],
                                                  s["p95_ms"], s["longest_gap_s"], outage))
