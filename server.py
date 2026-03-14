import asyncio
import aiohttp
import logging
import os
import random
import socket
import time
from itertools import cycle
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("proxy-rotator")

PROXIES_FILE = os.getenv("PROXIES_FILE", "proxies.txt")
ROTATION_MODE = os.getenv("ROTATION_MODE", "round-robin")  # round-robin | random
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8080))
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", 10))
AUTH_USER = os.getenv("AUTH_USER", "")
AUTH_PASS = os.getenv("AUTH_PASS", "")

# ── Proxy pool ──────────────────────────────────────────────────────────────

class ProxyPool:
    def __init__(self, path: str):
        self.path = path
        self.proxies: list[str] = []
        self._cycle = None
        self._lock = asyncio.Lock()
        self.stats: dict[str, dict] = {}  # per-proxy hit/fail counts
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            # Normalise — ensure scheme present
            normalised = []
            for line in lines:
                if not line.startswith("http"):
                    line = "http://" + line
                normalised.append(line)
            self.proxies = normalised
            self._cycle = cycle(self.proxies)
            log.info(f"Loaded {len(self.proxies)} proxies from {self.path}")
        except FileNotFoundError:
            log.warning(f"{self.path} not found — starting with empty pool")

    def reload(self):
        self.load()

    def next(self) -> str | None:
        if not self.proxies:
            return None
        if ROTATION_MODE == "random":
            return random.choice(self.proxies)
        return next(self._cycle)

    def record(self, proxy: str, success: bool):
        s = self.stats.setdefault(proxy, {"ok": 0, "fail": 0})
        if success:
            s["ok"] += 1
        else:
            s["fail"] += 1


pool = ProxyPool(PROXIES_FILE)


# ── Auth helper ─────────────────────────────────────────────────────────────

def check_proxy_auth(headers: dict) -> bool:
    if not AUTH_USER:
        return True
    import base64
    auth = headers.get(b"proxy-authorization", b"").decode(errors="ignore")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, _, passwd = decoded.partition(":")
        return user == AUTH_USER and passwd == AUTH_PASS
    except Exception:
        return False


# ── Core tunnel ─────────────────────────────────────────────────────────────

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


async def handle_connect(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    upstream: str | None,
):
    """Handle HTTPS CONNECT tunnelling, optionally via upstream proxy."""
    proxy = None
    up_reader = up_writer = None
    try:
        if upstream:
            parsed = urlparse(upstream)
            up_host = parsed.hostname
            up_port = parsed.port or 3128
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(up_host, up_port),
                timeout=CONNECT_TIMEOUT,
            )
            # Send our own CONNECT to the upstream proxy
            connect_req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n\r\n"
            ).encode()
            up_writer.write(connect_req)
            await up_writer.drain()
            # Read upstream response
            resp = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), timeout=CONNECT_TIMEOUT)
            if b"200" not in resp:
                raise ConnectionError(f"Upstream CONNECT failed: {resp[:80]}")
            proxy = upstream
        else:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port),
                timeout=CONNECT_TIMEOUT,
            )

        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        await asyncio.gather(
            pipe(client_reader, up_writer),
            pipe(up_reader, client_writer),
        )
        pool.record(upstream or "direct", True)
    except Exception as e:
        log.debug(f"CONNECT tunnel error ({target_host}:{target_port}): {e}")
        if upstream:
            pool.record(upstream, False)
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


async def handle_http(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    method: str,
    url: str,
    http_version: str,
    raw_headers: bytes,
    upstream: str | None,
):
    """Forward plain HTTP requests via upstream proxy or directly."""
    proxies = {upstream: upstream} if upstream else None
    try:
        timeout = aiohttp.ClientTimeout(total=30, connect=CONNECT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Rebuild headers dict
            headers = {}
            for line in raw_headers.split(b"\r\n"):
                if b": " in line:
                    k, _, v = line.partition(b": ")
                    key = k.decode(errors="ignore").lower()
                    if key not in ("proxy-authorization", "proxy-connection"):
                        headers[k.decode(errors="ignore")] = v.decode(errors="ignore")

            proxy_url = upstream if upstream else None
            async with session.request(
                method, url, headers=headers, proxy=proxy_url, allow_redirects=False
            ) as resp:
                status_line = f"HTTP/1.1 {resp.status} {resp.reason}\r\n"
                client_writer.write(status_line.encode())
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding",):
                        client_writer.write(f"{k}: {v}\r\n".encode())
                client_writer.write(b"\r\n")
                async for chunk in resp.content.iter_chunked(65536):
                    client_writer.write(chunk)
                await client_writer.drain()
        pool.record(upstream or "direct", True)
    except Exception as e:
        log.debug(f"HTTP forward error ({url}): {e}")
        if upstream:
            pool.record(upstream, False)
        try:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
        except Exception:
            pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


# ── Connection handler ───────────────────────────────────────────────────────

async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    try:
        raw = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=15)
    except Exception:
        client_writer.close()
        return

    lines = raw.split(b"\r\n")
    if not lines:
        client_writer.close()
        return

    request_line = lines[0].decode(errors="ignore")
    parts = request_line.split(" ")
    if len(parts) < 3:
        client_writer.close()
        return

    method, url, version = parts[0], parts[1], parts[2]
    headers_raw = b"\r\n".join(lines[1:])
    headers_dict = {}
    for line in lines[1:]:
        if b": " in line:
            k, _, v = line.partition(b": ")
            headers_dict[k.lower()] = v

    # Optional auth gate
    if not check_proxy_auth(headers_dict):
        client_writer.write(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="proxy"\r\n\r\n'
        )
        await client_writer.drain()
        client_writer.close()
        return

    upstream = pool.next()

    if method.upper() == "CONNECT":
        host, _, port = url.partition(":")
        port = int(port) if port else 443
        await handle_connect(client_reader, client_writer, host, port, upstream)
    else:
        await handle_http(client_reader, client_writer, method, url, version, headers_raw, upstream)


# ── Stats endpoint (tiny HTTP on PORT+1) ────────────────────────────────────

async def stats_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:
        pass
    total_ok = sum(v["ok"] for v in pool.stats.values())
    total_fail = sum(v["fail"] for v in pool.stats.values())
    body = (
        f"proxies_loaded: {len(pool.proxies)}\n"
        f"requests_ok: {total_ok}\n"
        f"requests_fail: {total_fail}\n"
        f"rotation_mode: {ROTATION_MODE}\n"
    )
    writer.write(
        f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode()
    )
    await writer.drain()
    writer.close()


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    log.info(f"Proxy rotator listening on {HOST}:{PORT}  (mode={ROTATION_MODE})")

    stats_port = PORT + 1
    try:
        stats_server = await asyncio.start_server(stats_handler, HOST, stats_port)
        log.info(f"Stats endpoint on {HOST}:{stats_port}")
    except OSError:
        stats_server = None
        log.warning(f"Could not bind stats port {stats_port}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
