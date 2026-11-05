#!/usr/bin/env python3
"""
run_eval.py - builds the Mininet network, runs every test and writes a report.

Needs the Ryu controller to be running FIRST (in another terminal, ryu venv):
    ryu-manager sdn_controller.py

Then, in a second terminal (NO venv, with sudo):
    sudo python3 run_eval.py

Output:  results/results.json  and  results/report.md   (plus logs/*.log)

Network built here (same shape as topo.py, with fixed IPs):
    h1..h4 (10.0.0.1-4) on s1 | r1 = resolver (10.0.0.100) on s2 | ns1..ns3 (10.0.0.11-13) on s3
    plus a root-namespace link on s2 (10.0.0.254) so the name servers can reach the
    controller's health socket through the switches.
"""
import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time

from common import (CONTROLLER_PORT, HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT, MAIN_NAMES,
                    NS_PORT, RESOLVER_PORT, ZONE, udp_request)

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOGDIR = os.path.join(HERE, "logs")
OUTDIR = os.path.join(HERE, "results")

CLIENT_IPS = {f"h{i}": f"10.0.0.{i}" for i in range(1, 5)}
R1_IP = "10.0.0.100"
NS_IPS = {"ns1": "10.0.0.11", "ns2": "10.0.0.12", "ns3": "10.0.0.13"}
ROOT_IP = "10.0.0.254"
CTRL_LOCAL = ("127.0.0.1", CONTROLLER_PORT)       # eval script talks to controller directly


# ------------------------------------------------------------------ helpers
def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def summarize(rows):
    lat = [r["latency_ms"] for r in rows if r["ok"]]
    n = len(rows)
    rnd = lambda x: None if x is None else round(x, 2)
    return {"n": n,
            "success_rate": round(sum(r["ok"] for r in rows) / n, 4) if n else 0.0,
            "mean_ms": rnd(statistics.mean(lat)) if lat else None,
            "p50_ms": rnd(pct(lat, 50)), "p95_ms": rnd(pct(lat, 95)),
            "p99_ms": rnd(pct(lat, 99))}


def failover_metrics(rows, t_kill):
    """rows = probe results; t_kill = time.time() when the server was killed."""
    before = [r["latency_ms"] for r in rows if r["t_send"] < t_kill and r["ok"]]
    base = statistics.median(before) if before else 1.0
    thr = max(50.0, base * 5)                     # "slow" = clearly worse than normal
    after = [r for r in rows if r["t_send"] >= t_kill]
    recovery = None
    for idx in range(len(after)):
        tail = after[idx:]
        if len(tail) >= 3 and all(r["ok"] and r["latency_ms"] <= thr for r in tail):
            recovery = round(after[idx]["t_send"] - t_kill, 2)
            break
    return {"baseline_ms": round(base, 2), "slow_threshold_ms": round(thr, 1),
            "queries_after_kill": len(after),
            "failed_queries": sum(not r["ok"] for r in after),
            "slow_queries": sum(r["latency_ms"] > thr for r in after),
            "worst_latency_ms": max((r["latency_ms"] for r in after), default=None),
            "recovery_s": recovery}


def collect(proc):
    out, err = proc.communicate()
    lines = [l for l in out.decode().splitlines() if l.startswith("[")]
    if not lines:
        raise RuntimeError("client produced no result: " + err.decode()[-400:])
    return json.loads(lines[-1])


# ------------------------------------------------------------------ the lab
class Lab:
    def __init__(self):
        self.net = None
        self.procs = {}

    # ---- topology
    def build(self):
        from mininet.net import Mininet
        from mininet.node import Node, OVSSwitch, RemoteController
        from mininet.link import TCLink

        net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                      autoSetMacs=True, autoStaticArp=False)
        net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
        s1, s2, s3 = [net.addSwitch(f"s{i}", protocols="OpenFlow13") for i in (1, 2, 3)]
        for name, ip in CLIENT_IPS.items():
            net.addLink(net.addHost(name, ip=f"{ip}/24"), s1)
        net.addLink(net.addHost("r1", ip=f"{R1_IP}/24"), s2)
        for name, ip in NS_IPS.items():
            net.addLink(net.addHost(name, ip=f"{ip}/24"), s3)
        net.addLink(s1, s2)
        net.addLink(s2, s3)
        root = Node("root", inNamespace=False)    # real machine side, runs Ryu
        intf = net.addLink(root, s2).intf1
        root.setIP(f"{ROOT_IP}/24", intf=intf)
        net.start()
        if hasattr(net, "waitConnected"):
            net.waitConnected()
        self.net = net
        self.root = root

    def wait_for_network(self):
        h1 = self.net.get("h1")
        for _ in range(30):
            out = h1.cmd(f"ping -c1 -W1 {R1_IP}")
            if " 0% packet loss" in out:
                return True
            time.sleep(1)
        return False

    # ---- processes
    def launch(self, key, host, cmd):
        log = open(os.path.join(LOGDIR, f"{key}.log"), "w")
        p = self.net.get(host).popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        self.procs[key] = (p, log)

    def stop_proc(self, key, sig=signal.SIGKILL):
        p, log = self.procs.pop(key, (None, None))
        if p is not None and p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        if log:
            log.close()

    def start_ns(self, name):
        self.stop_proc(name)
        self.launch(name, name, [PY, "-u", os.path.join(HERE, "nameserver.py"),
                                 "--name", name, "--port", str(NS_PORT),
                                 "--controller", f"{ROOT_IP}:{CONTROLLER_PORT}"])

    def start_resolver(self, mode, nocache=False):
        self.stop_proc("resolver")
        cmd = [PY, "-u", os.path.join(HERE, "resolver.py"), "--mode", mode,
               "--port", str(RESOLVER_PORT), "--timeout", "0.5",
               "--controller", f"{ROOT_IP}:{CONTROLLER_PORT}"]
        for name, ip in NS_IPS.items():
            cmd += ["--upstream", f"{name}={ip}:{NS_PORT}"]
        if nocache:
            cmd.append("--no-cache")
        self.launch("resolver", "r1", cmd)
        for _ in range(20):
            if self.resolver_stats() is not None:
                break
            time.sleep(0.25)
        time.sleep(1.0)                           # let the SDN poller get a first status

    # ---- talking to controller / resolver (from the root namespace)
    def ctl(self, obj):
        return udp_request(CTRL_LOCAL, obj, 1.0)

    def resolver_stats(self):
        return udp_request((R1_IP, RESOLVER_PORT), {"type": "stats"}, 0.5)

    def flush(self):
        udp_request((R1_IP, RESOLVER_PORT), {"type": "flush"}, 1.0)

    def servers(self):
        st = self.ctl({"type": "get_status"})
        return (st or {}).get("servers", {})

    def wait_servers_up(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            sv = self.servers()
            if all(sv.get(n, {}).get("up") for n in NS_IPS):
                return True
            time.sleep(0.3)
        return False

    def ensure_all_ns_up(self):
        sv = self.servers()
        for n in NS_IPS:
            if not sv.get(n, {}).get("up") or n not in self.procs:
                self.start_ns(n)
        return self.wait_servers_up()

    # ---- clients
    def client(self, host, names, count=0, duration=0, interval=0.05, timeout=3.0,
               rand=False, seed=1, background=False):
        cmd = [PY, os.path.join(HERE, "dns_client.py"), "--resolver", R1_IP,
               "--port", str(RESOLVER_PORT), "--names", ",".join(names),
               "--interval", str(interval), "--timeout", str(timeout), "--seed", str(seed)]
        if count:
            cmd += ["--count", str(count)]
        if duration:
            cmd += ["--duration", str(duration)]
        if rand:
            cmd.append("--random")
        p = self.net.get(host).popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p if background else collect(p)

    def cleanup(self):
        for key in list(self.procs):
            self.stop_proc(key)
        if self.net:
            self.net.stop()


# ------------------------------------------------------------------ experiments
def exp_basic_flow(lab):
    checks = []

    def add(test, passed, detail=""):
        checks.append({"test": test, "passed": bool(passed), "detail": detail})
        print(f"  [{'PASS' if passed else 'FAIL'}] {test} {detail}")

    lab.start_resolver("sdn")
    sv = lab.servers()
    add("controller receives heartbeats from ns1, ns2, ns3",
        all(sv.get(n, {}).get("up") for n in NS_IPS),
        str({n: sv.get(n, {}).get("up") for n in NS_IPS}))

    lab.flush()
    rows = lab.client("h1", list(ZONE), count=len(ZONE), interval=0.05)
    add("every name in the zone resolves to the right IP",
        all(r["ok"] and r["ip"] == ZONE[r["name"]][0] for r in rows))

    rows = lab.client("h1", ["nope.example.sdn"], count=1)
    add("unknown name returns NXDOMAIN", rows[0]["rcode"] == "NXDOMAIN", rows[0]["rcode"])

    lab.flush()
    rows = lab.client("h1", ["www.example.sdn"], count=2, interval=0.05)
    add("second lookup is served from the TTL cache",
        rows[1]["source"] == "cache", f"sources={[r['source'] for r in rows]}")

    lab.flush()
    ttl = ZONE["short.example.sdn"][1]
    rows = lab.client("h1", ["short.example.sdn"], count=2, interval=ttl + 0.7)
    add(f"cache entry expires after its TTL ({ttl}s)",
        rows[1]["source"] != "cache", f"sources={[r['source'] for r in rows]}")

    ok = True
    for h in CLIENT_IPS:
        r = lab.client(h, ["api.example.sdn"], count=1)
        ok = ok and r[0]["ok"]
    add("all four clients (h1-h4) can resolve through r1", ok)
    return checks


def exp_cache_latency(lab, mode):
    lab.start_resolver(mode)
    lab.flush()
    rows = lab.client("h1", MAIN_NAMES, count=300, interval=0.02, rand=True)
    st = lab.resolver_stats() or {}
    hit = [r["latency_ms"] for r in rows if r["ok"] and r["source"] == "cache"]
    miss = [r["latency_ms"] for r in rows if r["ok"] and r["source"] != "cache"]
    return {"mode": mode, "overall": summarize(rows),
            "cache_hit_rate": st.get("hit_rate"),
            "hit_mean_ms": round(statistics.mean(hit), 2) if hit else None,
            "miss_mean_ms": round(statistics.mean(miss), 2) if miss else None}


def exp_cold_latency(lab, mode):
    lab.start_resolver(mode, nocache=True)
    rows = lab.client("h1", MAIN_NAMES, count=200, interval=0.01, rand=True)
    return {"mode": mode, **summarize(rows)}


def exp_failover(lab, mode):
    lab.ensure_all_ns_up()
    lab.start_resolver(mode, nocache=True)        # no cache, so every probe hits a server
    lab.ctl({"type": "clear_events"})
    probe = lab.client("h1", ["www.example.sdn"], duration=10, interval=0.1,
                       timeout=3.0, background=True)
    time.sleep(2.0)
    t_kill = time.time()
    lab.stop_proc("ns1", signal.SIGKILL)          # hard kill: no goodbye message
    rows = collect(probe)
    events = (lab.ctl({"type": "get_events"}) or {}).get("events", [])
    down = [e for e in events if e["server"] == "ns1" and e["event"] == "down"]
    result = {"mode": mode, **failover_metrics(rows, t_kill),
              "controller_detect_s": round(down[0]["t"] - t_kill, 2) if down else None}
    lab.start_ns("ns1")
    lab.wait_servers_up()
    return result


def exp_loss(lab, mode, loss=40, warmup=12):
    lab.ensure_all_ns_up()
    lab.start_resolver(mode, nocache=True)
    ns1 = lab.net.get("ns1")
    dev = ns1.defaultIntf().name
    ns1.cmd(f"tc qdisc replace dev {dev} root netem loss {loss}%")
    time.sleep(warmup)                            # SDN needs a few heartbeats to notice
    est = lab.servers().get("ns1", {}).get("loss")
    rows = lab.client("h1", ["www.example.sdn"], duration=15, interval=0.05, timeout=3.0)
    ns1.cmd(f"tc qdisc del dev {dev} root")
    from_ns1 = sum(1 for r in rows if r["source"] == "ns1")
    return {"mode": mode, "netem_loss_pct": loss, "controller_loss_estimate": est,
            "answers_from_ns1_pct": round(100.0 * from_ns1 / len(rows), 1) if rows else None,
            **summarize(rows)}


# ------------------------------------------------------------------ report
ARCH = r"""
```
   h1    h2    h3    h4            clients  (dns_client.py)
     \    |     |    /
          [  s1  ]
             |
 r1 --------[  s2  ]-------- root namespace 10.0.0.254
 resolver.py   |             Ryu + sdn_controller.py
 (TTL cache)   |                  ^        ^
             [  s3  ]             | UDP 9999: heartbeats, events
             /  |  \              | (travel through the switches)
          ns1  ns2  ns3 ----------+
          nameserver.py
```
"""


def fmt(v, unit="", never="never"):
    return never if v is None else f"{v}{unit}"


def write_report(path, checks, cache, cold, failover, loss):
    L = []
    L.append("# SDN DNS-like Resolution - Resolver, Integration and Evaluation\n")
    L.append("## 1. Architecture\n")
    L.append(ARCH)
    L.append("**Components**\n")
    L.append("- `nameserver.py` - authoritative server for the zone; sends a UDP heartbeat "
             f"every {HEARTBEAT_INTERVAL}s and `started`/`stopping` events to the controller.")
    L.append("- `sdn_controller.py` - Ryu app: learning switch + health service. Marks a server "
             f"DOWN after {HEARTBEAT_TIMEOUT}s without heartbeats (or at once on `stopping`) and "
             "estimates per-server heartbeat loss over the last 20 heartbeats.")
    L.append("- `resolver.py` - TTL cache in front of the name servers. In `sdn` mode it polls the "
             "controller every 0.5s, skips servers marked DOWN and tries lossy servers last; in "
             "`static` mode it uses a fixed order and only reacts to timeouts.")
    L.append("- `dns_client.py` - query generator used on h1-h4.\n")
    L.append("**Normal flow:** client -> resolver -> (cache hit: answer) or (miss: ask first "
             "healthy name server, store answer for its TTL) -> client.\n")
    L.append("**Failure flow:** name server dies -> heartbeats stop -> controller marks it DOWN "
             "-> resolver sees the change on its next poll -> queries go straight to a healthy "
             "server.\n")

    L.append("## 2. Test cases - basic flow\n")
    L.append("| Test | Result | Detail |\n|---|---|---|")
    for c in checks:
        L.append(f"| {c['test']} | {'PASS' if c['passed'] else 'FAIL'} | {c['detail']} |")

    L.append("\n## 3. Performance results\n")
    L.append("### 3.1 Cache hit rate and latency (300 queries, 5 names, TTL 3s)\n")
    L.append("| Mode | Hit rate | Mean (hit) ms | Mean (miss) ms | p50 ms | p95 ms | Success |\n"
             "|---|---|---|---|---|---|---|")
    for r in cache:
        o = r["overall"]
        L.append(f"| {r['mode']} | {fmt(r['cache_hit_rate'])} | {fmt(r['hit_mean_ms'])} | "
                 f"{fmt(r['miss_mean_ms'])} | {fmt(o['p50_ms'])} | {fmt(o['p95_ms'])} | "
                 f"{o['success_rate']*100:.1f}% |")
    L.append("\n### 3.2 Latency without cache (healthy network, 200 queries)\n")
    L.append("| Mode | Mean ms | p50 ms | p95 ms | p99 ms | Success |\n|---|---|---|---|---|---|")
    for r in cold:
        L.append(f"| {r['mode']} | {fmt(r['mean_ms'])} | {fmt(r['p50_ms'])} | {fmt(r['p95_ms'])} | "
                 f"{fmt(r['p99_ms'])} | {r['success_rate']*100:.1f}% |")
    L.append("\n### 3.3 Failover (ns1 killed with SIGKILL 2s into a 10s probe, no cache)\n")
    L.append("| Mode | Controller detected after (s) | Recovered to normal latency after (s) | "
             "Failed queries | Slow queries | Worst latency ms |\n|---|---|---|---|---|---|")
    for r in failover:
        L.append(f"| {r['mode']} | {fmt(r['controller_detect_s'])} | "
                 f"{fmt(r['recovery_s'], never='never (within 10s probe)')} | {r['failed_queries']} | "
                 f"{r['slow_queries']} / {r['queries_after_kill']} | {fmt(r['worst_latency_ms'])} |")
    L.append("\n### 3.4 Packet loss on ns1 (tc netem, 15s probe after 12s warm-up, no cache)\n")
    L.append("| Mode | netem loss | Controller's loss estimate | Answers from ns1 | Mean ms | "
             "p95 ms | p99 ms | Success |\n|---|---|---|---|---|---|---|---|")
    for r in loss:
        L.append(f"| {r['mode']} | {r['netem_loss_pct']}% | {fmt(r['controller_loss_estimate'])} | "
                 f"{fmt(r['answers_from_ns1_pct'])}% | {fmt(r['mean_ms'])} | {fmt(r['p95_ms'])} | "
                 f"{fmt(r['p99_ms'])} | {r['success_rate']*100:.1f}% |")

    L.append("\n## 4. Observations (generated from the numbers above)\n")
    fo = {r["mode"]: r for r in failover}
    if "sdn" in fo and "static" in fo:
        L.append(f"- Failover: the controller noticed the dead server after "
                 f"{fmt(fo['sdn']['controller_detect_s'], 's')}. The SDN resolver returned to normal "
                 f"latency after {fmt(fo['sdn']['recovery_s'], 's', 'never')}; the static resolver "
                 f"{'returned after ' + str(fo['static']['recovery_s']) + 's' if fo['static']['recovery_s'] is not None else 'did not return to normal latency during the probe'} "
                 f"({fo['static']['slow_queries']} slow queries vs {fo['sdn']['slow_queries']}).")
    ch = {r["mode"]: r for r in cache}
    if ch:
        any_r = next(iter(ch.values()))
        L.append(f"- Cache: hit rate was {fmt(any_r['cache_hit_rate'])}; hits averaged "
                 f"{fmt(any_r['hit_mean_ms'], ' ms')} versus {fmt(any_r['miss_mean_ms'], ' ms')} for misses.")
    ls = {r["mode"]: r for r in loss}
    if "sdn" in ls and "static" in ls:
        L.append(f"- Packet loss: static sent {fmt(ls['static']['answers_from_ns1_pct'])}% of answers "
                 f"through the lossy ns1, SDN {fmt(ls['sdn']['answers_from_ns1_pct'])}%. "
                 f"p95 latency: static {fmt(ls['static']['p95_ms'], ' ms')}, "
                 f"SDN {fmt(ls['sdn']['p95_ms'], ' ms')}.")

    L.append("\n## 5. Limitations\n")
    L.append("- Everything runs in one Mininet VM, so latencies are small and reflect software "
             "overhead more than real network delay.")
    L.append("- Each experiment is a single run; repeat it a few times and average before quoting numbers.")
    L.append(f"- Detection time is bounded by the settings in `common.py` (heartbeat "
             f"{HEARTBEAT_INTERVAL}s, timeout {HEARTBEAT_TIMEOUT}s, resolver poll 0.5s).")
    L.append("- The static baseline is a resolver with a fixed server order and a 0.5s timeout; "
             "a baseline with smarter retry logic would narrow the gap.\n")
    with open(path, "w") as f:
        f.write("\n".join(L))


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-loss", action="store_true", help="skip the netem packet-loss test")
    ap.add_argument("--cli", action="store_true", help="open the Mininet CLI at the end")
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 run_eval.py")
    if udp_request(CTRL_LOCAL, {"type": "get_status"}, 1.0) is None:
        sys.exit("Controller health service not reachable on 127.0.0.1:%d.\n"
                 "Start it first (other terminal, ryu venv):  ryu-manager sdn_controller.py"
                 % CONTROLLER_PORT)
    os.makedirs(LOGDIR, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)

    from mininet.log import setLogLevel
    setLogLevel("warning")

    lab = Lab()
    try:
        print("Building network ...")
        lab.build()
        if not lab.wait_for_network():
            sys.exit("Hosts cannot reach each other - is Ryu running with sdn_controller.py?")
        for n in NS_IPS:
            lab.start_ns(n)
        if not lab.wait_servers_up():
            sys.exit("Name servers did not register with the controller (see logs/ns*.log).")

        print("\n[1/5] Basic flow tests")
        checks = exp_basic_flow(lab)
        print("\n[2/5] Cache hit rate and latency")
        cache = [exp_cache_latency(lab, m) for m in ("static", "sdn")]
        print("[3/5] Latency without cache")
        cold = [exp_cold_latency(lab, m) for m in ("static", "sdn")]
        print("[4/5] Failover (kill ns1)")
        failover = [exp_failover(lab, m) for m in ("static", "sdn")]
        loss = []
        if not a.skip_loss:
            print("[5/5] Packet loss on ns1 (netem)")
            loss = [exp_loss(lab, m) for m in ("static", "sdn")]

        results = {"checks": checks, "cache": cache, "cold": cold,
                   "failover": failover, "loss": loss}
        with open(os.path.join(OUTDIR, "results.json"), "w") as f:
            json.dump(results, f, indent=2)
        write_report(os.path.join(OUTDIR, "report.md"), checks, cache, cold, failover, loss)
        print(f"\nDone. See {OUTDIR}/report.md and results.json")
        if a.cli:
            from mininet.cli import CLI
            CLI(lab.net)
    finally:
        lab.cleanup()


if __name__ == "__main__":
    main()
