"""Single-PC HTTP relay; the PC retains authentication and CSRF enforcement."""
import asyncio
import base64
import binascii
import hmac
import ipaddress
import json
import os
import re
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, Response

DEFAULT_PUBLIC_URL = "https://super.tkcloud.online:444"
REQUEST_LIMIT = 128 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024
FRAME_LIMIT = 12 * 1024 * 1024
HOP_HEADERS = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade", b"content-length",
    b"accept-encoding",
}
HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def filtered_headers(headers, *, response=False):
    if not isinstance(headers, list) or len(headers) > 256:
        raise ValueError("Invalid headers")
    parsed = []
    size = 0
    for pair in headers:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not all(isinstance(v, str) for v in pair):
            raise ValueError("Invalid header pair")
        name, value = pair[0].encode("ascii").lower(), pair[1].encode("latin-1")
        size += len(name) + len(value)
        if size > 65536 or not HEADER_NAME.fullmatch(name) or any(c < 32 and c != 9 or c == 127 for c in value):
            raise ValueError("Invalid header value")
        parsed.append((name, value))
    excluded = set(HOP_HEADERS)
    for name, value in parsed:
        if name == b"connection":
            excluded.update(part.strip().lower() for part in value.split(b","))
    if not response:
        excluded.update({b"host", b"authorization", b"forwarded", b"x-forwarded-host", b"x-forwarded-proto", b"x-forwarded-for", b"x-real-ip", b"realip"})
    return [(name, value) for name, value in parsed if name not in excluded]


def create_app(token=None, public_url=None):
    token = os.environ.get("RELAY_TOKEN", "") if token is None else token
    public_url = os.environ.get("RELAY_PUBLIC_URL", DEFAULT_PUBLIC_URL) if public_url is None else public_url
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("RELAY_TOKEN must contain 32 to 256 URL-safe characters")
    origin = urlsplit(public_url)
    local_http = origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"}
    if (origin.scheme != "https" and not local_http) or not origin.hostname or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        raise ValueError("RELAY_PUBLIC_URL must be an HTTPS origin (HTTP allowed for loopback)")
    host = origin.netloc.encode("ascii")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    agent = None
    pending = {}
    active = 0
    send_lock = asyncio.Lock()

    def unavailable():
        return JSONResponse({"detail": "PC unavailable"}, status_code=503)

    @app.get("/_relay/health")
    async def health():
        return {"online": agent is not None}

    @app.websocket("/_relay/agent")
    async def connect(socket: WebSocket):
        nonlocal agent
        supplied = socket.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, ("Bearer " + token).encode("utf-8")):
            await socket.close(code=4401)
            return
        if agent is not None:
            await socket.close(code=4409)
            return
        await socket.accept()
        # Acceptance can yield: keep the single-agent decision atomic afterwards.
        if agent is not None:
            await socket.close(code=4409)
            return
        agent = socket
        try:
            while True:
                frame = await socket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                text = frame.get("text")
                if text is None:
                    await socket.close(code=1003)
                    break
                if len(text) > FRAME_LIMIT or len(text.encode("utf-8")) > FRAME_LIMIT:
                    await socket.close(code=1009)
                    break
                try:
                    message = json.loads(text)
                    if not isinstance(message, dict) or message.get("type") != "response" or not isinstance(message.get("id"), str):
                        raise ValueError("Expected response")
                    status = message.get("status")
                    body = message.get("body")
                    if type(status) is not int or not 200 <= status <= 599 or not isinstance(body, str) or len(body) > 4 * ((RESPONSE_LIMIT + 2) // 3):
                        raise ValueError("Invalid response")
                    decoded = base64.b64decode(body, validate=True)
                    if len(decoded) > RESPONSE_LIMIT:
                        raise ValueError("Response too large")
                    headers = filtered_headers(message.get("headers"), response=True)
                    future = pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result((status, headers, decoded))
                except (ValueError, TypeError, UnicodeError, binascii.Error):
                    await socket.close(code=1008)
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if agent is socket:
                agent = None
                for future in tuple(pending.values()):
                    if not future.done():
                        future.set_result(None)

    @app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(request: Request, path: str):
        nonlocal active
        decoded_path = request.url.path
        if not (decoded_path in {"/r", "/icon.svg"} or decoded_path.startswith(("/static/", "/api/"))) or "\\" in decoded_path or "//" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
            return Response(status_code=404)
        if request.method not in {"GET", "POST", "HEAD"}:
            return Response(status_code=405, headers={"Allow": "GET, POST, HEAD"})
        connection = agent
        if connection is None or active >= 32:
            return unavailable()
        active += 1
        identifier = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        pending[identifier] = future
        try:
            # Lucky appends REALIP on this private upstream. Ignore any earlier
            # visitor-supplied values, including repeated or comma-joined headers.
            forwarded = request.headers.getlist("realip") or request.headers.getlist("x-forwarded-for") or request.headers.getlist("x-real-ip")
            client_ip = forwarded[-1].rsplit(",", 1)[-1].strip() if forwarded else request.client.host
            address = ipaddress.ip_address(client_ip)
            client_ip = str(getattr(address, "ipv4_mapped", None) or address)
            body = bytearray()
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > REQUEST_LIMIT:
                        return Response(status_code=413)
                    body.extend(chunk)
            headers = filtered_headers([(name.decode("ascii"), value.decode("latin-1")) for name, value in request.headers.raw])
            headers.append((b"host", host))
            raw_path = request.scope.get("raw_path", decoded_path.encode("ascii"))
            target = raw_path.decode("ascii")
            if not target.startswith("/") or target.startswith("//") or "?" in target or "#" in target:
                return Response(status_code=400)
            query = request.scope.get("query_string", b"").decode("ascii")
            if query:
                target += "?" + query
            message = {"type": "request", "id": identifier, "method": request.method, "path": target, "client_ip": client_ip,
                       "headers": [[name.decode("ascii"), value.decode("latin-1")] for name, value in headers],
                       "body": base64.b64encode(body).decode("ascii")}
            timeout = 3615 if decoded_path == "/api/shell" or decoded_path.startswith("/api/commands/") and decoded_path.endswith("/run") else 30
            async with asyncio.timeout(timeout):
                async with send_lock:
                    if agent is not connection or future.done():
                        return unavailable()
                    await connection.send_json(message)
                result = await future
            if result is None:
                return unavailable()
            status, response_headers, response_body = result
            response = Response(content=response_body, status_code=status)
            response.raw_headers = response_headers + [(b"content-length", str(len(response_body)).encode("ascii"))]
            return response
        except TimeoutError:
            return JSONResponse({"detail": "PC request timed out"}, status_code=504)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return unavailable()
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        finally:
            pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            active -= 1

    return app
