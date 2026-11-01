"""UDP message format for the DNS-like resolution system.

Every datagram is one UTF-8 JSON object with a "type" field:

  QUERY     client -> server   {"type":"QUERY","id":42,"name":"www.example.com"}
  RESPONSE  server -> client   {"type":"RESPONSE","id":42,"name":"...","ip":"10.0.0.5","ttl":30,"server":"s1"}
  REDIRECT  server -> client   {"type":"REDIRECT","id":42,"name":"...","referral":"10.0.0.3:5300","server":"s1"}
  ERROR     server -> client   {"type":"ERROR","id":42,"code":"NXDOMAIN","msg":"...","server":"s1"}
  HB        server -> controller {"type":"HB","server":"ns1","port":5353,"load":12}   (load = queries since last heartbeat)

"id" lets the client match a response to its query (and ignore stale/duplicate ones).
"""
import json

MAX_DGRAM = 1024
Q, RESP, REDIR, ERR, HB = "QUERY", "RESPONSE", "REDIRECT", "ERROR", "HB"


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def decode(data: bytes) -> dict:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed datagram: {e}")
    if not isinstance(msg, dict) or "type" not in msg:
        raise ValueError("missing 'type'")
    return msg


def normalize(name: str) -> str:
    return name.strip().lower().rstrip(".")


def valid_name(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    return all(p and len(p) <= 63 and all(c.isalnum() or c in "-_" for c in p)
               for p in name.split("."))
