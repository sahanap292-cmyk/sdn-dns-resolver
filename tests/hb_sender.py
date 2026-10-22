import socket, json, time, sys
name = sys.argv[1]
load = int(sys.argv[2]) if len(sys.argv) > 2 else 0
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
print("heartbeat sender for", name, "load", load, flush=True)
while True:
    msg = json.dumps({"type": "HB", "server": name, "load": load}).encode()
    s.sendto(msg, ("10.0.0.254", 9999))
    time.sleep(2)
