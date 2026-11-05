"""
common.py - shared settings and tiny UDP/JSON helpers.

Member 3 part of the SDN DNS-like resolution project.
Everything talks JSON over UDP, so the name servers, resolver, client and
SDN controller all use the helpers below.
"""
import json
import socket

# ---- ports ----
CONTROLLER_PORT = 9999   # heartbeat / status socket inside the Ryu app
RESOLVER_PORT = 5300
NS_PORT = 5301

# ---- SDN health tracking ----
HEARTBEAT_INTERVAL = 0.5   # seconds between heartbeats from each name server
HEARTBEAT_TIMEOUT = 1.5    # no heartbeat for this long => controller marks server DOWN
LOSS_WINDOW = 20           # last N heartbeats used to estimate packet loss
DEGRADED_LOSS = 0.10       # above this loss ratio a server counts as "degraded"

# ---- the zone every name server serves: name -> (ip, ttl in seconds) ----
ZONE = {
    "www.example.sdn": ("10.1.0.10", 3),
    "mail.example.sdn": ("10.1.0.20", 3),
    "api.example.sdn": ("10.1.0.30", 3),
    "db.example.sdn": ("10.1.0.40", 3),
    "cdn.example.sdn": ("10.1.0.50", 3),
    "short.example.sdn": ("10.1.0.99", 2),   # tiny TTL, used to test cache expiry
}
MAIN_NAMES = ["www.example.sdn", "mail.example.sdn", "api.example.sdn",
              "db.example.sdn", "cdn.example.sdn"]


def udp_request(addr, obj, timeout):
    """Send one JSON message, wait for one JSON reply. Returns dict or None on timeout/error."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps(obj).encode(), addr)
        data, _ = s.recvfrom(65535)
        return json.loads(data.decode())
    except (socket.timeout, OSError, ValueError):
        return None
    finally:
        s.close()


def udp_send(addr, obj):
    """Fire-and-forget JSON message."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(obj).encode(), addr)
    except OSError:
        pass
    finally:
        s.close()


def parse_hostport(text, default_port=None):
    host, _, port = text.partition(":")
    return host, int(port) if port else default_port
