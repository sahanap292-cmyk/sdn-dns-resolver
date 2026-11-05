"""
health.py - the controller's view of the name servers (pure Python, no Ryu needed).

The Ryu app (sdn_controller.py) feeds heartbeats and events into a HealthTable and
answers status queries from it. Keeping it separate makes it unit-testable.
"""
import time

from common import HEARTBEAT_TIMEOUT, LOSS_WINDOW


class HealthTable:
    def __init__(self, timeout=HEARTBEAT_TIMEOUT, window=LOSS_WINDOW, clock=time.time):
        self.timeout = timeout
        self.window = window
        self.clock = clock
        self.servers = {}
        self.events = []

    def _log(self, name, event, reason=""):
        self.events.append({"t": self.clock(), "server": name,
                            "event": event, "reason": reason})

    def heartbeat(self, name, ip, port, seq, load=0):
        now = self.clock()
        s = self.servers.get(name)
        if s is None:
            s = {"ip": ip, "port": port, "up": False, "last_seen": 0.0,
                 "seqs": set(), "last_seq": -1, "load": 0, "loss": 0.0}
            self.servers[name] = s
        if seq < s["last_seq"]:            # sequence went backwards: server restarted
            s["seqs"] = set()
            s["last_seq"] = -1
        s.update(ip=ip, port=port, last_seen=now, load=load)
        s["seqs"].add(seq)
        s["last_seq"] = max(s["last_seq"], seq)
        low = s["last_seq"] - self.window
        s["seqs"] = {x for x in s["seqs"] if x > low}
        expected = min(self.window, s["last_seq"] + 1)
        s["loss"] = max(0.0, 1.0 - len(s["seqs"]) / expected)
        if not s["up"]:
            s["up"] = True
            self._log(name, "up", "heartbeat")

    def event(self, name, kind):
        """Explicit event from a server, e.g. 'started' or 'stopping'."""
        if kind == "stopping":
            self.mark_down(name, "graceful_shutdown")
        else:
            self._log(name, kind, "event")

    def mark_down(self, name, reason):
        s = self.servers.get(name)
        if s and s["up"]:
            s["up"] = False
            self._log(name, "down", reason)

    def check_timeouts(self):
        now = self.clock()
        for name, s in self.servers.items():
            if s["up"] and now - s["last_seen"] > self.timeout:
                self.mark_down(name, "heartbeat_timeout")

    def status(self):
        now = self.clock()
        return {n: {"ip": s["ip"], "port": s["port"], "up": s["up"],
                    "loss": round(s["loss"], 3), "load": s["load"],
                    "age": round(now - s["last_seen"], 2)}
                for n, s in self.servers.items()}
