"""Authenticated local client for the ordinary-user PowerPoint helper."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from helper_launcher import start_helper

MAX_RESPONSE = 8 * 1024 * 1024
MISSING = "PPT 模块未连接，请以普通用户运行 Start-PPT.bat 后刷新。"


class PowerPointBridge:
    def __init__(self, root: Path, logger: logging.Logger):
        self.root = Path(root).resolve()
        self.logger = logger
        self._start_lock = threading.Lock()
        self._last_start = 0.0
        self._closed = False
        self._startup_message = MISSING
        self._http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _record(self):
        try:
            data = (self.root / ".ppt-helper.json").read_bytes()
            if len(data) > 4096:
                raise ValueError("Invalid helper record")
            record = json.loads(data)
            if not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 0 or type(record.get("port")) is not int or not 1 <= record["port"] <= 65535 or not isinstance(record.get("token"), str) or not 32 <= len(record["token"]) <= 256 or not record["token"].isascii() or not all(char.isalnum() or char in "_-" for char in record["token"]):
                raise ValueError("Invalid helper record")
            return record
        except (OSError, ValueError, TypeError):
            raise RuntimeError(MISSING) from None

    def _request(self, path, body=None, *, timeout=30, record=None):
        if self._closed:
            raise RuntimeError("PPT 模块连接已关闭。")
        record = record or self._record()
        headers = {"Authorization": "Bearer " + record["token"]}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=True).encode("ascii")
        request = urllib.request.Request(f'http://127.0.0.1:{record["port"]}' + path, headers=headers, data=data)
        try:
            response = self._http.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError):
            raise RuntimeError(MISSING) from None
        with response:
            try:
                content = response.read(MAX_RESPONSE + 1)
            except OSError:
                raise RuntimeError("PPT 模块响应中断，请刷新重试。") from None
            if len(content) > MAX_RESPONSE:
                raise RuntimeError("PPT 模块响应过大。")
            media_type = response.headers.get_content_type()
            if response.status != 200:
                if response.status in {401, 403}:
                    raise RuntimeError("PPT 模块认证失败，请重启模块。")
                try:
                    message = json.loads(content).get("detail", "PPT 模块操作失败。")
                except (ValueError, AttributeError):
                    message = "PPT 模块返回无效错误。"
                if response.status == 400:
                    raise ValueError(message)
                raise RuntimeError(message)
            if media_type == "image/jpeg" and path.startswith("/image?"):
                return content
            try:
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid helper JSON")
            except ValueError:
                raise RuntimeError("PPT 模块响应格式无效。") from None
            if path in {"/state", "/health"}:
                if payload.get("module_connected") is not True or payload.get("module_elevated") is not False or payload.get("module_pid") != record["pid"]:
                    raise RuntimeError("PPT 模块身份或权限无效，请重新以普通用户启动。")
            return payload

    def start(self):
        with self._start_lock:
            if self._closed:
                return
            try:
                self._request("/health", timeout=2)
                return
            except RuntimeError:
                pass
            if time.monotonic() - self._last_start < 10:
                return
            self._last_start = time.monotonic()
            try:
                start_helper(self.root)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        self._request("/health", timeout=1)
                        self._startup_message = MISSING
                        self.logger.info("ppt.module.connected")
                        return
                    except RuntimeError:
                        threading.Event().wait(0.05)
                raise RuntimeError("PPT 模块未能启动，请查看 logs/ppt-helper.log 或普通运行 Start-PPT.bat。")
            except (RuntimeError, OSError) as error:
                self._startup_message = str(error)
                self.logger.warning("ppt.module.unavailable error=%s", self._startup_message)

    def state(self):
        try:
            return self._request("/state")
        except RuntimeError:
            self.start()
            try:
                return self._request("/state")
            except RuntimeError:
                return {"available": False, "message": self._startup_message, "name": "", "deck_id": "", "bookmark_id": "", "current_slide": 0, "slideshow": False, "width": 0, "height": 0, "slides": [], "module_connected": False, "module_elevated": False, "module_pid": None}

    def image(self, slide, deck, size, refresh=""):
        query = urllib.parse.urlencode({"slide": slide, "deck": deck, "size": size, "refresh": refresh})
        content = self._request("/image?" + query)
        if not isinstance(content, bytes):
            raise RuntimeError("PPT 模块没有返回图片。")
        return content

    def action(self, action):
        return self._request("/action", {"action": action})

    def goto(self, slide, deck):
        return self._request("/goto", {"slide": slide, "deck": deck})

    def close(self):
        with self._start_lock:
            if self._closed:
                return
            try:
                # Always authenticate the module identity before requesting its shutdown.
                self._request("/health", timeout=2)
                self._request("/shutdown", {}, timeout=3)
            except (RuntimeError, OSError):
                pass
            finally:
                self._closed = True
