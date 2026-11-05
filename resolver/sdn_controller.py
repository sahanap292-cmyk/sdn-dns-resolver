"""
sdn_controller.py - Ryu app: normal learning switch + name-server health service.

Run (from this folder, inside the ryu venv):
    ryu-manager sdn_controller.py

What it adds on top of simple_switch_13:
  * a UDP socket (port 9999) where name servers send heartbeats and events
  * a health table (up/down, estimated heartbeat loss per server)
  * a tiny query API used by the resolver and the evaluation script:
        {"type": "get_status"}   -> health of every server
        {"type": "get_events"}   -> list of up/down events with timestamps
        {"type": "clear_events"} -> empty the event list
"""
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ryu.app.simple_switch_13 import SimpleSwitch13   # noqa: E402
from ryu.lib import hub                                # noqa: E402

from common import CONTROLLER_PORT                     # noqa: E402
from health import HealthTable                         # noqa: E402


class SdnDnsController(SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.table = HealthTable()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", CONTROLLER_PORT))
        hub.spawn(self._listen)
        hub.spawn(self._monitor)
        self.logger.info("health service listening on UDP %d", CONTROLLER_PORT)

    def _listen(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            reply = self._handle(msg, addr)
            if reply is not None:
                self.sock.sendto(json.dumps(reply).encode(), addr)

    def _handle(self, msg, addr):
        kind = msg.get("type")
        if kind == "heartbeat":
            was_up = self.table.servers.get(msg["name"], {}).get("up", False)
            self.table.heartbeat(msg["name"], addr[0], msg.get("port"),
                                 msg.get("seq", 0), msg.get("load", 0))
            if not was_up:
                self.logger.info("server %s is UP (%s)", msg["name"], addr[0])
        elif kind == "event":
            self.table.event(msg["name"], msg.get("event", ""))
            self.logger.info("event from %s: %s", msg["name"], msg.get("event"))
        elif kind == "get_status":
            return {"type": "status", "servers": self.table.status()}
        elif kind == "get_events":
            return {"type": "events", "events": self.table.events}
        elif kind == "clear_events":
            self.table.events.clear()
            return {"type": "ok"}
        return None

    def _monitor(self):
        while True:
            before = {n for n, s in self.table.servers.items() if s["up"]}
            self.table.check_timeouts()
            for n in before:
                if not self.table.servers[n]["up"]:
                    self.logger.warning("server %s is DOWN (no heartbeat)", n)
            hub.sleep(0.25)
