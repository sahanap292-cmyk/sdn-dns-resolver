import json, socket, sys
host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5353
kind = sys.argv[3] if len(sys.argv) > 3 else "stats"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(json.dumps({"type": kind}).encode(), (host, port))
print(s.recvfrom(65535)[0].decode())
