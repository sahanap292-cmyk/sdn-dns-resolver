import socket
t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t.connect(('10.0.0.254', 9))
me = t.getsockname()[0]
t.close()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('', 5353))
print("echo server on", me, "UDP 5353", flush=True)
while True:
    data, addr = s.recvfrom(1024)
    s.sendto(('ECHO from ' + me).encode(), addr)
