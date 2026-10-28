"""Outbound WSS tunnel; LAN traffic continues to use the same FastAPI app."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import suppress
import ipaddress
import json
from urllib.parse import unquote, urlsplit

import httpx
from websockets.asyncio.client import connect

DEFAULT_RELAY_URL = "https://super.tkcloud.online:444"
MAX_BODY = 128 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_FRAME = 12 * 1024 * 1024
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding", "host", "authorization"}


def relay_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("中继端口无效。") from error
    loopback = parsed.hostname == "localhost"
    with suppress(ValueError):
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("中继地址须为 HTTPS 源地址；本机调试可用 HTTP。")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("中继端口无效。")
    return value


def allowed_path(path: str) -> bool:
    if not isinstance(path, str) or len(path) > 8192 or not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(char) < 32 for char in path):
        return False
    parsed = urlsplit(path)
    decoded = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        return False
    return decoded in {"/", "/r", "/icon.svg"} or decoded.startswith(("/api/", "/static/"))


class RelayClient:
    def __init__(self, app, store, logger):
        self.app, self.store, self.logger = app, store, logger
        self.connected = False
        self.status = "未连接"
        self.loop = None
        self.changed = asyncio.Event()
        self.inflight = set()

    def public_state(self):
        config = self.store.snapshot().relay
        return {"enabled": config.enabled, "url": config.url, "connected": self.connected, "status": self.status if config.enabled else "已禁用", "token_configured": bool(config.token)}

    def reconfigure(self):
        loop = self.loop
        if loop is not None:
            # Allow a settings response to leave the current tunnel before reconnecting.
            loop.call_soon_threadsafe(lambda: loop.call_later(0.3, self.changed.set))

    def set_status(self, connected, status):
        if (connected, status) != (self.connected, self.status):
            self.logger.info("relay.state connected=%s status=%s", connected, status)
        self.connected, self.status = connected, status

    async def run(self):
        self.loop = asyncio.get_running_loop()
        delay = 1
        try:
            while True:
                self.changed.clear()
                config = self.store.snapshot().relay
                if not config.enabled or not config.token:
                    self.set_status(False, "已禁用" if not config.enabled else "未配置密钥")
                    await self.changed.wait()
                    continue
                address = urlsplit(config.url)
                endpoint = ("wss" if address.scheme == "https" else "ws") + "://" + address.netloc + "/_relay/agent"
                try:
                    self.set_status(False, "连接中")
                    async with connect(endpoint, additional_headers={"Authorization": "Bearer " + config.token}, max_size=MAX_FRAME, open_timeout=10, close_timeout=3, ping_interval=20, ping_timeout=20, proxy=None, compression=None) as websocket:
                        self.set_status(True, "已连接")
                        delay = 1
                        exchange = asyncio.create_task(self.exchange(websocket, config.url))
                        changed = asyncio.create_task(self.changed.wait())
                        try:
                            done, _ = await asyncio.wait({exchange, changed}, return_when=asyncio.FIRST_COMPLETED)
                            if exchange in done:
                                await exchange
                        finally:
                            exchange.cancel()
                            changed.cancel()
                            await asyncio.gather(exchange, changed, return_exceptions=True)
                    self.set_status(False, "未连接")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Do not log URLs, frames, authorization headers or exception payloads.
                    self.set_status(False, "连接失败，等待重试")
                    self.logger.warning("relay.connect.failed type=%s", type(error).__name__)
                if self.changed.is_set():
                    continue
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.changed.wait(), timeout=delay)
                delay = min(delay * 2, 30)
        finally:
            self.set_status(False, "未连接")
            self.loop = None

    async def exchange(self, websocket, origin):
        active_ids = set()
        attached = True
        send_lock = asyncio.Lock()
        transport = httpx.ASGITransport(app=self.app, client=("relay", 0), raise_app_exceptions=False)

        async def send(message):
            if not attached:
                return
            async with send_lock:
                await websocket.send(json.dumps(message, ensure_ascii=True, separators=(",", ":")))

        async def forward(message):
            identity = message["id"]
            try:
                path = message.get("path")
                method = message.get("method")
                if method not in {"GET", "POST", "HEAD"} or not allowed_path(path):
                    raise ValueError("Invalid forwarded target")
                encoded = message.get("body", "")
                if not isinstance(encoded, str) or len(encoded) > (MAX_BODY + 2) // 3 * 4:
                    raise ValueError("Request body too large")
                body = base64.b64decode(encoded, validate=True)
                if len(body) > MAX_BODY:
                    raise ValueError("Request body too large")
                raw_headers = message.get("headers", [])
                if not isinstance(raw_headers, list) or len(raw_headers) > 128:
                    raise ValueError("Invalid forwarded headers")
                headers = []
                for pair in raw_headers:
                    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) for value in pair) or any("\r" in value or "\n" in value for value in pair):
                        raise ValueError("Invalid forwarded header")
                    name, value = pair
                    if name.lower() not in HOP_HEADERS:
                        headers.append((name, value))
                # A fresh cookie jar per request prevents one remote browser inheriting another's login.
                async with httpx.AsyncClient(transport=transport, base_url=origin, follow_redirects=False, timeout=None, trust_env=False) as client:
                    response = await client.request(method, path, headers=headers, content=body)
                data = response.content
                if len(data) > MAX_RESPONSE:
                    raise ValueError("Response body too large")
                response_headers = [[name, value] for name, value in response.headers.multi_items() if name.lower() not in HOP_HEADERS]
                await send({"type": "response", "id": identity, "status": response.status_code, "headers": response_headers, "body": base64.b64encode(data).decode("ascii")})
            except asyncio.CancelledError:
                raise
            except (ValueError, binascii.Error, httpx.HTTPError):
                await send({"type": "response", "id": identity, "status": 502, "headers": [["content-type", "application/json"]], "body": base64.b64encode(b'{"detail":"Relay request failed"}').decode()})
            finally:
                active_ids.discard(identity)

        try:
            async for raw in websocket:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("type") != "request" or not isinstance(message.get("id"), str) or not 1 <= len(message["id"]) <= 128 or message["id"] in active_ids:
                    raise ValueError("Invalid relay message")
                if len(self.inflight) >= 32:
                    await send({"type": "response", "id": message["id"], "status": 503, "headers": [], "body": ""})
                    continue
                active_ids.add(message["id"])
                task = asyncio.create_task(forward(message))
                self.inflight.add(task)
                def finished(completed):
                    self.inflight.discard(completed)
                    if not completed.cancelled() and completed.exception() is not None:
                        self.logger.warning("relay.response.failed type=%s", type(completed.exception()).__name__)
                task.add_done_callback(finished)
        finally:
            # An ASGI sync endpoint can shield cancellation until its command finishes.
            # Let bounded requests finish independently so reconnect never waits for them.
            attached = False
