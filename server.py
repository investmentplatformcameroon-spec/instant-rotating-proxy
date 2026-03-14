import asyncio
import base64
import logging
import os
import random
from itertools import cycle
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("proxy-rotator")

PROXIES_FILE    = os.getenv("PROXIES_FILE", "proxies.txt")
ROTATION_MODE   = os.getenv("ROTATION_MODE", "round-robin")
HOST            = os.getenv("HOST", "0.0.0.0")
PORT            = int(os.getenv("PORT", 8080))
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", 10))
AUTH_USER       = os.getenv("AUTH_USER", "")
AUTH_PASS       = os.getenv("AUTH_PASS", "")

# ── Proxy pool ───────────────────────────────────────────────────────────────

class ProxyPool:
    def __init__(self, path: str):
        self.proxies: list[dict] = []
        self._cycle = None
        self.load(path)

    def load(self, path: str):
        try:
            with open(path) as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            result = []
            for line in lines:
                p = self._parse(line)
                if p:
                    result.append(p)
            self.proxies = result
            self._cycle = cycle(self.proxies)
            log.info(f"Loaded {len(self.proxies)} proxies from {path}")
        except FileNotFoundError:
            log.warning(f"{path} not found")

    def _parse(self, line: str) -> dict | None:
        try:
            if not line.startswith("http"):
                parts = line.split(":")
                if len(parts) == 4:
                    return {"host": parts[0], "port": int(parts[1]), "user": parts[2], "passwd": parts[3]}
                if len(parts) == 2:
                    return {"host": parts[0], "port": int(parts[1]), "user": "", "passwd": ""}
            u = urlparse(line)
            return {"host": u.hostname, "port": u.port or 3128, "user": u.username or "", "passwd": u.password or ""}
        except Exception:
            return None

    def next(self) -> dict | None:
        if not self.proxies:
            return None
        if ROTATION_MODE == "random":
            return random.choice(self.proxies)
        return next(self._cycle)


pool = ProxyPool(PROXIES_FILE)

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_proxy_auth_header(proxy: dict) -> str:
    if proxy["user"]:
        creds = base64.b64encode(f"{proxy['user']}:{proxy['passwd']}".encode()).decode()
        return f"Proxy-Authorization: Basic {creds}\r\n"
    return ""

def check_client_auth(headers: dict) -> bool:
    if not AUTH_USER:
        return True
    auth = headers.get(b"proxy-authorization", b"").decode(errors="ignore")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, _, passwd = decoded.partition(":")
        return user == AUTH_USER and passwd == AUTH_PASS
    except Exception:
        return False

async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

# ── HTTPS CONNECT tunnel ─────────────────────────────────────────────────────

async def handle_connect(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    proxy: dict,
):
    up_reader = up_writer = None
    try:
        # Open TCP to upstream proxy
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(proxy["host"], proxy["port"]),
            timeout=CONNECT_TIMEOUT,
        )

        # Send CONNECT with Proxy-Authorization — this is what was missing
        auth = make_proxy_auth_header(proxy)
        request = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n"
            f"{auth}"
            f"\r\n"
        ).encode()
        up_writer.write(request)
        await up_writer.drain()

        # Read upstream response
        response = await asyncio.wait_for(
            up_reader.readuntil(b"\r\n\r\n"),
            timeout=CONNECT_TIMEOUT,
        )

        if b"200" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"Upstream rejected CONNECT: {response[:100]}")

        # Tell client tunnel is open
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        log.info(f"CONNECT {target_host}:{target_port} via {proxy['host']}:{proxy['port']}")

        # Pipe data both ways
        await asyncio.gather(
            pipe(client_reader, up_writer),
            pipe(up_reader, client_writer),
        )

    except Exception as e:
        log.warning(f"CONNECT failed ({target_host}:{target_port} via {proxy['host']}): {e}")
        try:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
        except Exception:
            pass
    finally:
        for w in (client_writer, up_writer):
            if w:
                try:
                    w.close()
                except Exception:
                    pass

# ── HTTP forward ─────────────────────────────────────────────────────────────

async def handle_http(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    first_line: str,
    headers_raw: bytes,
    proxy: dict,
):
    up_reader = up_writer = None
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(proxy["host"], proxy["port"]),
            timeout=CONNECT_TIMEOUT,
        )

        # Strip client proxy headers, inject upstream auth
        clean_headers = []
        for line in headers_raw.split(b"\r\n"):
            lower = line.lower()
            if lower.startswith(b"proxy-authorization") or lower.startswith(b"proxy-connection"):
                continue
            if line:
                clean_headers.append(line)

        auth = make_proxy_auth_header(proxy)
        if auth:
            clean_headers.append(auth.strip().encode())

        request = (first_line + "\r\n").encode()
        request += b"\r\n".join(clean_headers)
        request += b"\r\n\r\n"

        up_writer.write(request)
        await up_writer.drain()

        log.info(f"HTTP {first_line} via {proxy['host']}:{proxy['port']}")

        await asyncio.gather(
            pipe(up_reader, client_writer),
            pipe(client_reader, up_writer),
        )

    except Exception as e:
        log.warning(f"HTTP failed: {e}")
        try:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
        except Exception:
            pass
    finally:
        for w in (client_writer, up_writer):
            if w:
                try:
                    w.close()
                except Exception:
                    pass

# ── Main connection handler ───────────────────────────────────────────────────

async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    try:
        raw = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=15)
    except Exception:
        try:
            client_writer.close()
        except Exception:
            pass
        return

    lines = raw.split(b"\r\n")
    if not lines:
        client_writer.close()
        return

    first_line = lines[0].decode(errors="ignore").strip()
    parts = first_line.split(" ")
    if len(parts) < 2:
        client_writer.close()
        return

    method = parts[0].upper()
    url = parts[1]
    headers_raw = b"\r\n".join(lines[1:])

    headers_dict = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers_dict[k.strip().lower()] = v.strip()

    if not check_client_auth(headers_dict):
        client_writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Proxy-Authenticate: Basic realm=\"proxy\"\r\n\r\n"
        )
        await client_writer.drain()
        client_writer.close()
        return

    proxy = pool.next()
    if not proxy:
        client_writer.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    if method == "CONNECT":
        host, _, port = url.partition(":")
        port = int(port) if port else 443
        await handle_connect(client_reader, client_writer, host, port, proxy)
    else:
        await handle_http(client_reader, client_writer, first_line, headers_raw, proxy)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    log.info(f"Proxy rotator listening on {HOST}:{PORT} (mode={ROTATION_MODE})")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
