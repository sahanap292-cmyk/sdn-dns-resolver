import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(b'hello', (sys.argv[1], 5353))
try:
    print(s.recvfrom(1024))
except socket.timeout:
    print("TIMEOUT: no reply")
