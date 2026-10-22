import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('', 5353))
print("echo server listening on UDP 5353", flush=True)
while True:
    data, addr = s.recvfrom(1024)
    s.sendto(b'ECHO ' + data, addr)
