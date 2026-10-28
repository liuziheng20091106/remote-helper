"""FastAPI control service and the application's only native surface: a tray icon."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, field_validator
import uvicorn

from windows_control import WindowsController
from relay_client import DEFAULT_RELAY_URL, RelayClient, relay_url
from ppt_bridge import PowerPointBridge
from touchpad_control import TouchpadController
from music_service import MusicService
from music_confirmation import MusicConfirmation

ROOT = Path(__file__).resolve().parent
COOKIE = "remote_helper_session"
OUTPUT_LIMIT = 1024 * 1024


class UISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desktop_zoom: float = Field(default=1, ge=1, le=4)
    desktop_interval: float = Field(default=1.5, ge=0.5, le=10)
    desktop_quality: int = Field(default=65, ge=30, le=90)
    desktop_width: int = Field(default=1280, ge=320, le=2560)


class ShellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=32768)
    shell: Literal["cmd", "powershell"] = "cmd"
    timeout: int = Field(default=60, ge=1, le=3600)


class CommandRequest(ShellRequest):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)


class SavedCommand(ShellRequest):
    id: str
    name: str = Field(min_length=1, max_length=120)


class RelaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    url: str = DEFAULT_RELAY_URL
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(36), min_length=32, max_length=256, repr=False, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return relay_url(value)


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: int = Field(default=8766, ge=1, le=65535)
    password: str = Field(default="114514", min_length=1, max_length=256)
    web_paused: bool = False
    settings: UISettings = Field(default_factory=UISettings)
    relay: RelaySettings = Field(default_factory=RelaySettings)
    commands: list[SavedCommand] = Field(default_factory=list)
    favorites: list[str] = Field(default_factory=list)


class Store:
    def __init__(self, root: Path):
        self.path = root / "settings.json"
        self.lock = threading.RLock()
        data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        # Remove the retired display preference when upgrading an existing installation.
        if isinstance(data.get("settings"), dict):
            data["settings"].pop("round_mode", None)
        self.config = Configuration.model_validate(data)
        if not self.path.exists() or data != self.config.model_dump():
            self.save(self.config)

    def save(self, candidate: Configuration):
        # Commit memory only after durable replacement; failed saves keep the previous state.
        with self.lock:
            temporary = self.path.with_suffix(".json.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(candidate.model_dump_json(indent=2))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self.config = candidate

    def snapshot(self) -> Configuration:
        with self.lock:
            return self.config.model_copy(deep=True)

    def update(self, **changes):
        with self.lock:
            data = self.config.model_dump()
            data.update(changes)
            self.save(Configuration.model_validate(data))


def make_logger(root: Path) -> logging.Logger:
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    logger = logging.getLogger(f"remote-helper.{root}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(logs / "operations.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


class ShellRunner:
    def __init__(self, root: Path, logger: logging.Logger):
        self.root, self.logger = root, logger
        self.condition = threading.Condition()
        self.active: set[subprocess.Popen] = set()
        self.stopping = False

    @staticmethod
    def stop_tree(process):
        if process.poll() is not None:
            return
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=False)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def shutdown(self):
        with self.condition:
            self.stopping = True
            active = list(self.active)
        for process in active:
            self.logger.info("shell.shutdown pid=%s", process.pid)
            self.stop_tree(process)
        with self.condition:
            if not self.condition.wait_for(lambda: not self.active, timeout=30):
                raise RuntimeError("命令工作线程未能结束，取消重启。")

    def run(self, body: ShellRequest) -> dict:
        started = time.monotonic()
        if body.shell == "powershell":
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            args = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + body.command]
        else:
            executable = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
            # cmd.exe parses /c itself; CRT argv escaping corrupts embedded quotes.
            args = f'{executable} /d /s /c "chcp 65001>nul & {body.command}"'
        self.logger.info("shell.start shell=%s timeout=%s command=%r", body.shell, body.timeout, body.command)
        timed_out = False
        with tempfile.TemporaryFile() as output:
            with self.condition:
                if self.stopping:
                    raise RuntimeError("程序正在关闭，不能启动新命令。")
                process = subprocess.Popen(args, cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                self.active.add(process)
            try:
                try:
                    process.wait(timeout=body.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self.stop_tree(process)
                output.seek(0)
                data = output.read(OUTPUT_LIMIT + 1)
            finally:
                with self.condition:
                    self.active.discard(process)
                    self.condition.notify_all()
        result = {"output": data[:OUTPUT_LIMIT].decode("utf-8", errors="replace"), "returncode": process.returncode, "duration": round(time.monotonic() - started, 3), "timed_out": timed_out, "truncated": len(data) > OUTPUT_LIMIT}
        self.logger.info("shell.finish pid=%s code=%s duration=%s timeout=%s truncated=%s", process.pid, result["returncode"], result["duration"], timed_out, result["truncated"])
        return result


class VolumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: float | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None


class FavoriteRequest(BaseModel):
    key: str = Field(min_length=1, max_length=32768)
    favorite: bool


class TerminateRequest(BaseModel):
    create_time: float = Field(gt=0)
    force: bool = False


class ClickRequest(BaseModel):
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"
    clicks: Literal[1, 2] = 1


class KeyRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=8)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class ScrollRequest(BaseModel):
    amount: int = Field(ge=-100, le=100)


class BodyLimit:
    """Bound ASGI chunks before Starlette buffers a request body."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > 128 * 1024:
                    raise HTTPException(413, "请求内容过大。")
            return message

        await self.app(scope, bounded_receive if scope["type"] == "http" else receive, send)


class PPTActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["previous", "next", "start", "start_current", "end", "first", "last", "black", "white"]


class PPTGotoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slide: int = Field(ge=1)
    deck: str = Field(min_length=1, max_length=128)


class TouchpadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    seq: int = Field(ge=0, le=9007199254740991, strict=True)
    events: list[dict] = Field(default_factory=list, max_length=64)
    release: bool = Field(default=False, strict=True)


class MusicPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    song_id: int = Field(gt=0, le=2**63 - 1, strict=True)


class MusicTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(root: Path = ROOT, *, music_cookie_path: Path | None = None) -> FastAPI:
    store = Store(root)
    logger = make_logger(root)
    controller = WindowsController(root)
    powerpoint = PowerPointBridge(root, logger)
    touchpad = TouchpadController(controller)
    music = MusicService(root, logger, cookie_path=music_cookie_path)
    confirmation = MusicConfirmation(music)
    sessions: set[str] = set()
    failed_login_ips: set[str] = set()
    session_lock = threading.Lock()
    @asynccontextmanager
    async def lifespan(application):
        ppt_start = asyncio.create_task(asyncio.to_thread(powerpoint.start), name="ppt-helper-start")
        task = asyncio.create_task(tunnel.run(), name="outbound-relay")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            confirmation.close()
            await asyncio.to_thread(touchpad.close)
            await asyncio.to_thread(music.close)
            await asyncio.to_thread(runner.shutdown)
            await asyncio.gather(*tunnel.inflight, return_exceptions=True)
            await ppt_start
            await asyncio.to_thread(powerpoint.close)

    app = FastAPI(title="Remote Helper", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False, lifespan=lifespan)
    app.state.store, app.state.logger, app.state.controller = store, logger, controller
    app.state.powerpoint = powerpoint
    app.state.touchpad, app.state.music, app.state.music_confirmation = touchpad, music, confirmation
    runner = ShellRunner(root, logger)
    app.state.shell = runner
    tunnel = RelayClient(app, store, logger)
    app.state.relay = tunnel

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        path = request.url.path
        if path == "/":
            return Response(status_code=404)
        with session_lock:
            authenticated = request.cookies.get(COOKIE, "") in sessions
        if path.startswith("/api/") and request.method != "GET":
            if int(request.headers.get("content-length", "0")) > 128 * 1024:
                return JSONResponse({"detail": "请求内容过大。"}, status_code=413)
            # Drain small bodies even when rejecting: Windows otherwise resets the connection.
            try:
                async with asyncio.timeout(10):
                    await request.body()
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
            except TimeoutError:
                return JSONResponse({"detail": "请求上传超时。"}, status_code=408)
        if store.snapshot().web_paused:
            return JSONResponse({"detail": "Web 管理已暂停，请从电脑托盘恢复。"}, status_code=503)
        if path.startswith("/api/"):
            if not authenticated:
                return JSONResponse({"detail": "请输入访问密码登录。"}, status_code=401)
            if request.method != "GET":
                origin = request.headers.get("origin")
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if request.headers.get("x-remote-helper") != "1" or (origin and origin != expected):
                    return JSONResponse({"detail": "请求来源校验失败。"}, status_code=403)
                logger.info("request client=%s action=%s %s", request.client.host if request.client else "?", request.method, path)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if path.startswith("/api/") and response.status_code >= 400:
            logger.warning("request.failed action=%s status=%s", path, response.status_code)
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        logger.exception("operation.failed path=%s", request.url.path, exc_info=exc)
        return JSONResponse({"detail": f"操作失败：{exc}"}, status_code=500)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        logger.warning("operation.invalid path=%s error=%s", request.url.path, exc)
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def unavailable(request: Request, exc: RuntimeError):
        logger.warning("operation.unavailable path=%s error=%s", request.url.path, exc)
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def denied(request: Request, exc: PermissionError):
        logger.warning("operation.denied path=%s error=%s", request.url.path, exc)
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.get("/r")
    def remote(request: Request, p: str | None = Query(default=None, max_length=256)):
        response = FileResponse(root / "static/index.html")
        if p is not None:
            address = ipaddress.ip_address(request.client.host)
            client_ip = str(getattr(address, "ipv4_mapped", None) or address)
            with session_lock:
                sessions.discard(request.cookies.get(COOKIE, ""))
                if client_ip in failed_login_ips:
                    response = JSONResponse({"detail": "该 IP 本次运行期间已登录失败，重启程序前禁止再次登录。"}, status_code=429)
                elif not hmac.compare_digest(p.encode("utf-8"), store.snapshot().password.encode("utf-8")):
                    failed_login_ips.add(client_ip)
                    logger.warning("login.failed client=%s blocked_until_restart=true", client_ip)
                    response = JSONResponse({"detail": "密码错误。该 IP 在本次程序运行期间已禁止再次登录。"}, status_code=401)
                else:
                    logger.info("login.success client=%s", client_ip)
                    session = secrets.token_urlsafe(48)
                    sessions.add(session)
                    response.set_cookie(COOKIE, session, httponly=True, samesite="strict", secure=request.url.scheme == "https")
                    return response
            response.delete_cookie(COOKIE)
        return response

    @app.get("/icon.svg")
    def icon():
        return FileResponse(root / "icon.svg", media_type="image/svg+xml")

    @app.get("/api/state")
    def state():
        config = store.snapshot()
        return {"hostname": socket.gethostname(), "platform": "Windows", "admin": is_admin(), "port": config.port, "paused": config.web_paused, "settings": config.settings.model_dump(), "commands": [item.model_dump() for item in config.commands], "favorites": config.favorites, "relay": tunnel.public_state()}

    @app.post("/api/settings")
    def settings(body: dict):
        with store.lock:
            updated = store.config.settings.model_dump()
            updated.update(body)
            try:
                validated = UISettings.model_validate(updated)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            store.update(settings=validated.model_dump())
        logger.info("settings.saved fields=%s", list(body))
        return validated.model_dump()

    @app.post("/api/relay")
    def configure_relay(body: dict):
        if set(body) - {"enabled", "url", "token"}:
            raise HTTPException(422, "无效中继设置。")
        with store.lock:
            updated = store.config.relay.model_dump()
            if not body.get("token"):
                body.pop("token", None)
            updated.update(body)
            try:
                candidate = RelaySettings.model_validate(updated)
            except ValueError:
                raise HTTPException(422, "请检查中继地址及密钥（32–256 位字母、数字、下划线或连字符）。") from None
            store.update(relay=candidate.model_dump())
        logger.info("relay.settings.saved fields=%s", list(body))
        tunnel.reconfigure()
        return tunnel.public_state()

    @app.get("/api/commands")
    def commands():
        return store.snapshot().commands

    @app.post("/api/commands")
    def save_command(body: CommandRequest):
        if not body.command.strip() or not body.name.strip():
            raise ValueError("名称和命令不能为空。")
        with store.lock:
            items = [item.model_dump() for item in store.config.commands]
            if body.id is not None and not any(item["id"] == body.id for item in items):
                raise HTTPException(404, "快捷命令不存在。")
            command = {**body.model_dump(), "id": body.id or secrets.token_hex(8)}
            items = [command if item["id"] == command["id"] else item for item in items]
            if body.id is None:
                items.append(command)
            store.update(commands=items)
        logger.info("command.saved id=%s name=%r", command["id"], body.name)
        return command

    def find_command(command_id: str) -> SavedCommand:
        item = next((item for item in store.snapshot().commands if item.id == command_id), None)
        if item is None:
            raise HTTPException(404, "快捷命令不存在。")
        return item

    @app.post("/api/commands/{command_id}/delete")
    def delete_command(command_id: str):
        with store.lock:
            find_command(command_id)
            store.update(commands=[item.model_dump() for item in store.config.commands if item.id != command_id])
        logger.info("command.deleted id=%s", command_id)
        return {"ok": True}

    @app.post("/api/commands/{command_id}/run")
    def execute_command(command_id: str):
        return runner.run(find_command(command_id))

    @app.post("/api/shell")
    def shell(body: ShellRequest):
        if not body.command.strip():
            raise ValueError("命令不能为空。")
        return runner.run(body)

    @app.get("/api/shortcuts")
    def shortcuts():
        return controller.shortcuts()

    @app.post("/api/shortcuts/{shortcut_id}/launch")
    def launch(shortcut_id: str):
        result = controller.launch_shortcut(shortcut_id)
        logger.info("shortcut.launched name=%r path=%r", result.get("name"), result.get("path"))
        return result

    @app.get("/api/volume")
    def volume():
        return controller.volume()

    @app.post("/api/volume")
    def change_volume(body: VolumeRequest):
        result = controller.volume(body.level, body.muted)
        logger.info("volume.changed %s", result)
        return result

    @app.get("/api/processes")
    def processes():
        return controller.processes(store.snapshot().favorites)

    @app.post("/api/processes/favorite")
    def favorite(body: FavoriteRequest):
        with store.lock:
            favorites = set(store.config.favorites)
            favorites.add(body.key) if body.favorite else favorites.discard(body.key)
            store.update(favorites=sorted(favorites))
        logger.info("process.favorite key=%r favorite=%s", body.key, body.favorite)
        return {"ok": True}

    @app.post("/api/processes/{pid}/terminate")
    def terminate(pid: int, body: TerminateRequest):
        result = controller.terminate(pid, body.create_time, body.force)
        logger.info("process.terminated pid=%s created=%s force=%s", pid, body.create_time, body.force)
        return result

    @app.get("/api/desktop/info")
    def desktop_info():
        return controller.desktop_info()

    @app.get("/api/desktop/screenshot")
    def screenshot(monitor: int = Query(default=1, ge=0), width: int = Query(default=1280, ge=320, le=2560), quality: int = Query(default=65, ge=30, le=90)):
        data, geometry = controller.screenshot(monitor, width, quality)
        headers = {"X-Desktop-" + key.title(): str(geometry[key]) for key in ("left", "top", "width", "height")}
        headers.update({"X-Image-Width": str(geometry["image_width"]), "X-Image-Height": str(geometry["image_height"])})
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.post("/api/desktop/click")
    def click(body: ClickRequest):
        result = controller.click(body.x, body.y, body.button, body.clicks)
        logger.info("desktop.click x=%s y=%s button=%s clicks=%s", body.x, body.y, body.button, body.clicks)
        return result

    @app.post("/api/desktop/key")
    def key(body: KeyRequest):
        result = controller.key(body.keys)
        logger.info("desktop.key keys=%s", body.keys)
        return result

    @app.post("/api/desktop/text")
    def text(body: TextRequest):
        result = controller.text(body.text)
        logger.info("desktop.text characters=%s", len(body.text))
        return result

    @app.post("/api/desktop/scroll")
    def scroll(body: ScrollRequest):
        result = controller.scroll(body.amount)
        logger.info("desktop.scroll amount=%s", body.amount)
        return result

    @app.post("/api/desktop/touchpad")
    def touchpad_input(request: Request, body: TouchpadRequest):
        with controller._input_lock:
            with session_lock:
                if request.cookies[COOKIE] not in sessions:
                    raise HTTPException(401, "请输入访问密码登录。")
            if store.snapshot().web_paused:
                raise HTTPException(503, "Web 管理已暂停，请从电脑托盘恢复。")
            owner = request.cookies[COOKIE] + ":" + body.id
            return touchpad.apply(owner, body.seq, body.events, body.release)

    def music_state():
        return {**music.state(), **confirmation.time_state()}

    @app.get("/api/music/state")
    def get_music_state():
        return music_state()

    @app.get("/api/music/search")
    def music_search(q: str = Query(min_length=1, max_length=200), offset: int = Query(default=0, ge=0, le=10000), limit: int = Query(default=20, ge=1, le=50)):
        return music.search(q, offset, limit)

    @app.post("/api/music/import")
    def music_import():
        result = music.import_credentials()
        logger.info("music.credentials.imported cookies=%s", result.get("cookie_count", 0))
        return {**result, **confirmation.time_state()}

    @app.post("/api/music/prepare")
    def music_prepare(request: Request, body: MusicPrepareRequest):
        return confirmation.prepare(request.cookies[COOKIE], body.song_id)

    @app.get("/api/music/confirmation/{ticket}")
    def music_confirmation_status(request: Request, ticket: str):
        return confirmation.status(request.cookies[COOKIE], ticket)

    @app.post("/api/music/confirm")
    def music_confirm(request: Request, body: MusicTicketRequest):
        result = confirmation.confirm(request.cookies[COOKIE], body.ticket)
        logger.info("music.play.confirmed")
        return {**result, **confirmation.time_state()}

    @app.post("/api/music/cancel")
    def music_cancel(request: Request, body: MusicTicketRequest):
        return confirmation.cancel(request.cookies[COOKIE], body.ticket)

    @app.post("/api/music/stop")
    def music_stop(request: Request):
        result = confirmation.stop(request.cookies[COOKIE])
        logger.info("music.stopped")
        return {**result, **confirmation.time_state()}

    @app.get("/api/ppt/state")
    def ppt_state():
        return powerpoint.state()

    @app.get("/api/ppt/slides/{slide}")
    def ppt_image(slide: int, deck: str = Query(min_length=1, max_length=128), size: Literal["thumb", "large"] = "thumb", refresh: str = Query(default="", max_length=64)):
        if slide < 1:
            raise ValueError("页码须大于零。")
        data = powerpoint.image(slide, deck, size, refresh=refresh)
        return Response(data, media_type="image/jpeg")

    @app.post("/api/ppt/action")
    def ppt_action(body: PPTActionRequest):
        result = powerpoint.action(body.action)
        logger.info("ppt.action action=%s slide=%s", body.action, result.get("current_slide"))
        return result

    @app.post("/api/ppt/goto")
    def ppt_goto(body: PPTGotoRequest):
        result = powerpoint.goto(body.slide, body.deck)
        logger.info("ppt.goto slide=%s", body.slide)
        return result

    @app.get("/api/logs")
    def logs():
        path = root / "logs/operations.log"
        with path.open(encoding="utf-8", errors="replace") as stream:
            return {"lines": list(deque(stream, maxlen=200))}

    @app.post("/api/logout")
    def logout(request: Request):
        session = request.cookies.get(COOKIE, "")
        try:
            with controller._input_lock:
                try:
                    touchpad.release_session(session)
                finally:
                    with session_lock:
                        sessions.discard(session)
        finally:
            confirmation.cancel(session)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    app.add_middleware(BodyLimit)
    return app


def tray_image(paused=False) -> Image.Image:
    # The tray uses the same hand-drawn geometry as icon.svg, rasterized with Pillow.
    image = Image.new("RGBA", (128, 128))
    draw = ImageDraw.Draw(image)
    base, accent, light = "#10272b", "#ffc66d", "#65e4c5"
    draw.rounded_rectangle((4, 4, 124, 124), radius=30, fill=base)
    draw.rounded_rectangle((23, 26, 105, 85), radius=9, outline=light, width=7)
    draw.line((64, 87, 64, 100), fill=light, width=7)
    draw.line((46, 103, 82, 103), fill=light, width=7)
    draw.line((42, 45, 53, 55, 42, 65), fill=accent, width=6, joint="curve")
    draw.line((64, 65, 82, 65), fill=accent, width=6)
    draw.ellipse((84, 70, 118, 104), fill=base)
    draw.ellipse((88, 74, 114, 100), fill=accent)
    if paused:
        draw.line((97, 82, 97, 92), fill=base, width=4)
        draw.line((105, 82, 105, 92), fill=base, width=4)
    else:
        draw.line((95, 87, 99, 91, 107, 82), fill=base, width=4, joint="curve")
    return image


class Runtime:
    def __init__(self, app: FastAPI):
        self.app = app
        config = app.state.store.snapshot()
        self.server = uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port, access_log=False, log_config=None, proxy_headers=False, timeout_graceful_shutdown=5))
        self.thread: threading.Thread | None = None
        self.icon = None
        self.restarting = False
        self.stopping = False
        self.failure: Exception | None = None
        server_logger = logging.getLogger("uvicorn.error")
        server_logger.handlers = list(app.state.logger.handlers)
        server_logger.propagate = False
        server_logger.setLevel(logging.INFO)

    def pause(self, icon=None, item=None):
        store = self.app.state.store
        with self.app.state.controller._input_lock:
            paused = not store.snapshot().web_paused
            store.update(web_paused=paused)
            if paused:
                self.app.state.touchpad.release_all()
        self.app.state.logger.info("web.paused=%s", paused)
        if self.icon:
            self.icon.icon = tray_image(paused)
            self.icon.title = "Remote Helper · " + ("已暂停" if paused else "运行中")
            self.icon.update_menu()

    def shutdown(self, icon=None, item=None):
        if self.stopping:
            return
        self.stopping = True
        self.app.state.logger.info("application.stopping restart=%s", self.restarting)
        self.server.should_exit = True
        try:
            self.app.state.shell.shutdown()
        except Exception as error:
            self.failure = error
            self.app.state.logger.exception("shell.shutdown.failed")
        finally:
            if self.icon:
                self.icon.stop()

    def restart(self, icon=None, item=None):
        self.restarting = True
        self.shutdown()

    def serve(self):
        try:
            self.server.run()
        except (Exception, SystemExit) as error:
            self.app.state.logger.exception("web.server.failed")
            config = self.app.state.store.snapshot()
            self.failure = RuntimeError(f"Web 服务启动或运行失败（{config.host}:{config.port}）：{error}。请查看 logs/operations.log。")
        finally:
            if self.icon:
                self.icon.stop()

    def run(self, headless=False):
        config = self.app.state.store.snapshot()
        self.app.state.logger.info("application.start host=%s port=%s admin=%s paused=%s", config.host, config.port, is_admin(), config.web_paused)
        if headless:
            try:
                self.serve()
            finally:
                self.shutdown()
            if self.failure:
                raise self.failure
            return
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Remote Helper · 电脑遥控", None, enabled=False),
            pystray.MenuItem(f"端口 {config.port} · /r?p=访问密码", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: "恢复 Web 管理" if self.app.state.store.snapshot().web_paused else "暂停 Web 管理", self.pause),
            pystray.MenuItem("重启程序", self.restart),
            pystray.MenuItem("退出程序", self.shutdown),
        )
        self.icon = pystray.Icon("RemoteHelper", tray_image(config.web_paused), "Remote Helper · " + ("已暂停" if config.web_paused else "运行中"), menu)
        self.thread = threading.Thread(target=self.serve, name="web-server", daemon=True)
        web_thread = self.thread

        def setup(icon):
            icon.visible = True
            web_thread.start()

        try:
            self.icon.run(setup=setup)
        finally:
            self.shutdown()
            if self.thread.ident is not None:
                self.thread.join(timeout=15)
                if self.thread.is_alive():
                    raise RuntimeError("Web 服务尚未停止，取消重启。")
        if self.failure:
            raise self.failure


def acquire_instance(root: Path):
    import hashlib
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    name = "Local\\RemoteHelper-" + hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    if error == 183:
        kernel.CloseHandle(handle)
        raise RuntimeError("此目录的 Remote Helper 已在运行，请使用现有托盘图标。")
    return kernel, handle


def main():
    parser = argparse.ArgumentParser(description="Remote Helper background service")
    parser.add_argument("--headless", action="store_true", help="Run without tray for service diagnostics")
    args = parser.parse_args()
    os.chdir(ROOT)
    kernel, handle = acquire_instance(ROOT)
    runtime = None
    try:
        image = tray_image()
        image.save(ROOT / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
        runtime = Runtime(create_app())
        runtime.run(args.headless)
    finally:
        kernel.CloseHandle(handle)
    if runtime and runtime.restarting:
        subprocess.Popen([sys.executable, str(ROOT / "main.py"), *(["--headless"] if args.headless else [])], cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        details = traceback.format_exc()
        with (ROOT / "startup-error.log").open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + details + "\n")
        if sys.stderr is not None:
            sys.stderr.write(details)
        ctypes.windll.user32.MessageBoxW(None, "启动失败，请查看当前目录 startup-error.log。\n" + details.splitlines()[-1], "Remote Helper", 0x10)
        sys.exit(1)
