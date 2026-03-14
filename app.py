import os
import random
import threading
import socket
import select
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler

def load_proxies(filepath="proxies.txt"):
    proxies = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                proxies.append({
                    "host": parts[0],
                    "port": int(parts[1]),
                    "user": parts[2],
                    "pass": parts[3]
                })
    return proxies

proxies = load_proxies()

def get_proxy():
    return random.choice(proxies)

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence logs

    def do_CONNECT(self):
        proxy = get_proxy()
        try:
            # Connect to upstream proxy
            sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=15)
            # Send CONNECT with auth to upstream
            auth = base64.b64encode(f"{proxy['user']}:{proxy['pass']}".encode()).decode()
            connect_req = (
                f"CONNECT {self.path} HTTP/1.1\r\n"
                f"Host: {self.path}\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n\r\n"
            )
            sock.sendall(connect_req.encode())
            response = b""
            while b"\r\n\r\n" not in response:
                response += sock.recv(4096)
            self.send_response(200, "Connection Established")
            self.end_headers()
            # Tunnel traffic
            self._tunnel(self.connection, sock)
        except Exception as e:
            self.send_error(502, str(e))

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        proxy = get_proxy()
        try:
            sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=15)
            auth = base64.b64encode(f"{proxy['user']}:{proxy['pass']}".encode()).decode()
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            request_line = f"{self.command} {self.path} HTTP/1.1\r\n"
            headers = f"Proxy-Authorization: Basic {auth}\r\n"
            for k, v in self.headers.items():
                headers += f"{k}: {v}\r\n"
            headers += "\r\n"
            sock.sendall((request_line + headers).encode() + body)
            self._tunnel(self.connection, sock)
        except Exception as e:
            self.send_error(502, str(e))

    def _tunnel(self, client, upstream):
        client.setblocking(False)
        upstream.setblocking(False)
        while True:
            r, _, _ = select.select([client, upstream], [], [], 10)
            if not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is client else client).sendall(data)
                except:
                    return

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"Proxy rotator running on port {port} with {len(proxies)} proxies")
    server.serve_forever()
