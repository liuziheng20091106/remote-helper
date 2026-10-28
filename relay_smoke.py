"""Real HTTPS/WSS relay check. Run with the app venv; requires openssl on PATH."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import httpx
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from main import ROOT, Store, create_app
from relay.server import create_app as create_relay


class Server:
    def __init__(self, app, tls=None):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        options = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}
        self.server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None, ws_max_size=12 * 1024 * 1024, timeout_graceful_shutdown=3, **options))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Smoke server failed to start")
            threading.Event().wait(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()
        assert not self.thread.is_alive()


async def eventually(check):
    deadline = time.monotonic() + 15
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("Relay state did not converge")
        await asyncio.sleep(0.04)


async def check():
    token = secrets.token_urlsafe(36)
    with tempfile.TemporaryDirectory(prefix="remote-helper-relay-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "static", root / "static")
        shutil.copyfile(ROOT / "icon.svg", root / "icon.svg")
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        context = ssl.create_default_context(cafile=str(cert))
        old_ca = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(cert)
        relay_socket = socket.socket()
        relay_socket.bind(("127.0.0.1", 0))
        relay_port = relay_socket.getsockname()[1]
        relay_socket.close()
        remote_url = f"https://127.0.0.1:{relay_port}"
        relay_app = create_relay(token=token, public_url=remote_url)
        remote = Server(relay_app, tls=(cert, key))
        remote.socket.close()
        remote.socket = socket.socket()
        remote.socket.bind(("127.0.0.1", relay_port))
        remote.port = relay_port
        remote.thread = threading.Thread(target=remote.server.run, kwargs={"sockets": [remote.socket]}, daemon=True)
        Store(root).update(relay={"enabled": False, "url": remote_url, "token": token})
        pc_app = create_app(root)
        pc = Server(pc_app)
        lan_url = f"http://127.0.0.1:{pc.port}"
        remote.start()
        pc.start()
        header = {"X-Remote-Helper": "1"}
        try:
            async with httpx.AsyncClient(base_url=remote_url, verify=context, trust_env=False, timeout=15) as browser, httpx.AsyncClient(base_url=lan_url, trust_env=False, timeout=15) as lan:
                assert (await browser.get("/_relay/health")).json() == {"online": False}
                assert (await browser.get("/api/state")).status_code == 503
                assert (await lan.get("/api/state")).status_code == 401
                await lan.get("/r?p=114514")
                assert (await lan.get("/api/state")).status_code == 200
                configured = await lan.post("/api/relay", json={"enabled": True}, headers=header)
                assert configured.status_code == 200 and "token" not in configured.json()
                async def online():
                    return (await browser.get("/_relay/health")).json()["online"]
                await eventually(online)
                # The websocket credential is independent from the webpage password.
                ws_url = remote_url.replace("https:", "wss:") + "/_relay/agent"
                for credential in ("wrong-token", token):
                    try:
                        async with connect(ws_url, additional_headers={"Authorization": "Bearer " + credential}, ssl=context, proxy=None) as duplicate:
                            await duplicate.recv()
                        raise AssertionError("Unauthenticated or duplicate agent was accepted")
                    except (InvalidStatus, Exception) as error:
                        # Both pre-accept HTTP rejection and post-accept policy close are valid.
                        from websockets.exceptions import ConnectionClosed
                        assert isinstance(error, (InvalidStatus, ConnectionClosed)), type(error)
                assert (await browser.get("/api/state")).status_code == 401
                login = await browser.get("/r?p=114514")
                assert login.status_code == 200
                cookie = next(item for item in browser.cookies.jar if item.name == "remote_helper_session")
                assert cookie.secure and cookie.has_nonstandard_attr("HttpOnly")
                state = (await browser.get("/api/state")).json()
                assert state["hostname"] and "token" not in state["relay"]
                ppt = await browser.get("/api/ppt/state")
                assert ppt.status_code == 200 and isinstance(ppt.json()["available"], bool)
                assert type(ppt.json()["module_connected"]) is bool
                assert ppt.json()["module_elevated"] is False
                assert (await browser.post("/api/ppt/action", json={"action": "next"})).status_code == 403
                if not ppt.json()["available"]:
                    missing = await browser.post("/api/ppt/goto", json={"slide": 1, "deck": "old"}, headers={**header, "Origin": remote_url})
                    assert missing.status_code == 409
                else:
                    image = await browser.get("/api/ppt/slides/1", params={"deck": ppt.json()["deck_id"], "size": "thumb"})
                    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
                assert (await browser.get("/static/app.js")).status_code == 200
                icon = await browser.get("/icon.svg")
                assert icon.content == (ROOT / "icon.svg").read_bytes()
                async with httpx.AsyncClient(base_url=remote_url, verify=context, trust_env=False) as stranger:
                    assert (await stranger.get("/api/state")).status_code == 401, "Tunnel must not share cookies between browsers"
                assert (await browser.post("/api/shell", json={"command": "echo BLOCKED"})).status_code == 403
                assert (await browser.post("/api/shell", json={"command": "echo BLOCKED"}, headers={**header, "Origin": "https://attacker.example"})).status_code == 403
                mutation_headers = {**header, "Origin": remote_url}
                output = await browser.post("/api/shell", json={"command": "echo RELAY_TLS_OK", "timeout": 10}, headers=mutation_headers)
                assert output.status_code == 200 and "RELAY_TLS_OK" in output.json()["output"], output.text
                slow = asyncio.create_task(browser.post("/api/shell", json={"shell": "powershell", "command": "Start-Sleep -Seconds 2; Write-Output CONCURRENT_OK", "timeout": 10}, headers=mutation_headers))
                await asyncio.sleep(0.2)
                started = time.monotonic()
                assert (await browser.get("/api/state")).status_code == 200
                assert time.monotonic() - started < 1.5, "Slow command blocked other relay requests"
                assert "CONCURRENT_OK" in (await slow).json()["output"]
                saved = await browser.post("/api/settings", json={"desktop_zoom": 2.5}, headers=mutation_headers)
                assert saved.status_code == 200 and Store(root).snapshot().settings.desktop_zoom == 2.5
                pc_app.state.store.update(web_paused=True)
                assert (await browser.get("/api/state")).status_code == 503
                assert (await lan.get("/api/state")).status_code == 503
                pc_app.state.store.update(web_paused=False)
                assert (await browser.get("/api/state")).status_code == 200
                logged_out_token = cookie.value
                assert (await browser.post("/api/logout", json={}, headers=mutation_headers)).status_code == 200
                browser.cookies.set("remote_helper_session", logged_out_token)
                assert (await browser.get("/api/state")).status_code == 401
                browser.cookies.clear()
                await browser.get("/r?p=114514")
                marker = root / "reconnect-command.started"
                code = f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('started'); time.sleep(10)"
                pending_command = asyncio.create_task(browser.post("/api/shell", json={"command": subprocess.list2cmdline([str(ROOT / '.venv/Scripts/python.exe'), '-c', code]), "timeout": 30}, headers=mutation_headers))
                async def command_started():
                    return marker.exists()
                await eventually(command_started)
                disconnected_at = time.monotonic()
                assert (await lan.post("/api/relay", json={"enabled": False}, headers=header)).status_code == 200
                async def offline():
                    return not (await browser.get("/_relay/health")).json()["online"]
                await eventually(offline)
                assert time.monotonic() - disconnected_at < 3, "Active ASGI command delayed tunnel close"
                assert (await pending_command).status_code == 503
                assert (await browser.get("/api/state")).status_code == 503
                assert (await lan.get("/api/state")).status_code == 200
                assert (await lan.post("/api/relay", json={"enabled": True}, headers=header)).status_code == 200
                await eventually(online)
                assert (await browser.get("/api/state")).status_code == 200
                assert time.monotonic() - disconnected_at < 5, "Reconnect waited for the old command to finish"
                assert pc_app.state.shell.active, "Old command should still be running while remote access recovers"
                pc_app.state.shell.shutdown()
                async def oversized():
                    yield b"x" * 70000
                    yield b"y" * 70000
                assert (await browser.post("/api/shell", content=oversized(), headers=mutation_headers)).status_code == 413
                assert (await browser.get("/settings.json")).status_code == 404
                print("PASS: actual HTTPS/WSS tunnel, independent agent auth, duplicate rejection, browser cookie isolation/Secure/HttpOnly/logout, Origin/CSRF, real shell and concurrent requests, bytes/headers, persisted settings, pause, disconnect/reconnect, upload bound, LAN stays available.")
        finally:
            pc_app.state.shell.shutdown()
            pc.stop()
            remote.stop()
            for handler in list(pc_app.state.logger.handlers):
                handler.close()
                pc_app.state.logger.removeHandler(handler)
            if old_ca is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = old_ca


if __name__ == "__main__":
    asyncio.run(check())
