r"""Run: .venv\Scripts\python.exe smoke_check.py. Real Windows/HTTP checks, isolated settings."""
from __future__ import annotations

import http.cookiejar
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image
import psutil
import uvicorn

from main import COOKIE, ROOT, Store, create_app


def check():
    with tempfile.TemporaryDirectory(prefix="remote-helper-check-") as directory:
        root = Path(directory)
        (root / "static").mkdir()
        (root / "static/index.html").write_text("<html>HTTP smoke</html>", encoding="utf-8")
        (root / "icon.svg").write_bytes((ROOT / "icon.svg").read_bytes())
        (root / "Fix Tools").mkdir()
        (root / "Fix Tools/sample.url").write_text("[InternetShortcut]\nURL=https://example.com\n", encoding="utf-8")
        (root / "Fix Tools/ignored.reg").write_text("not a shortcut", encoding="utf-8")
        # Upgrade an existing config without restoring the removed view preference.
        (root / "settings.json").write_text(json.dumps({"settings": {"round_mode": True, "desktop_zoom": 1.5}, "relay": {"enabled": False}}), encoding="utf-8")
        app = create_app(root)
        assert "round_mode" not in json.loads((root / "settings.json").read_text("utf-8"))["settings"]
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("Smoke server failed to start")
            threading.Event().wait(0.02)
        cookies = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        child = None

        def request(path, body=None, *, authenticated=True, csrf=True, origin=None):
            headers = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
                if csrf:
                    headers["X-Remote-Helper"] = "1"
            if origin:
                headers["Origin"] = origin
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
            opener = client if authenticated else urllib.request.build_opener()
            try:
                response = opener.open(req, timeout=30)
            except urllib.error.HTTPError as error:
                response = error
            data = response.read()
            kind = response.headers.get("Content-Type", "")
            return response.status, json.loads(data) if "application/json" in kind else data, response.headers

        try:
            assert request("/api/state", authenticated=False)[0] == 401
            request("/r?p=wrong")
            assert not list(cookies)
            request("/r?p=114514")
            assert any(cookie.name == COOKIE and cookie.has_nonstandard_attr("HttpOnly") for cookie in cookies)
            assert request("/api/state")[0] == 200
            chunked = urllib.request.Request(base + "/api/unknown", data=iter([b"x" * 70000, b"y" * 70000]), headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(chunked, timeout=10)
                raise AssertionError("Chunked upload exceeded the body limit")
            except urllib.error.HTTPError as error:
                assert error.code == 413
                error.close()
            with socket.create_connection(("127.0.0.1", sock.getsockname()[1]), timeout=15) as slow:
                slow.sendall(b"POST /api/state HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n1\r\nx\r\n")
                assert b" 408 " in slow.recv(4096), "Incomplete uploads must have a deadline"
            assert request("/api/shell", {"command": "echo forbidden"}, csrf=False)[0] == 403
            assert request("/api/shell", {"command": "echo forbidden"}, origin="http://other.example")[0] == 403
            status, result, _ = request("/api/shell", {"command": "echo REMOTE_HELPER_OK", "shell": "cmd", "timeout": 10})
            assert status == 200 and result["returncode"] == 0 and "REMOTE_HELPER_OK" in result["output"]
            status, result, _ = request("/api/shell", {"command": "[Console]::Write('中文✓')", "shell": "powershell", "timeout": 10})
            assert status == 200 and result["output"] == "中文✓", result
            _, result, _ = request("/api/shell", {"command": "ping -n 10 127.0.0.1 >nul", "timeout": 1})
            assert result["timed_out"] and result["returncode"] != 0
            command = subprocess.list2cmdline([sys.executable, "-c", "print('x'*1100000)"])
            _, result, _ = request("/api/shell", {"command": command, "timeout": 10})
            assert result["truncated"] and len(result["output"]) == 1024 * 1024
            _, saved, _ = request("/api/commands", {"name": "smoke", "command": "echo SAVED_OK", "shell": "cmd", "timeout": 10})
            command_id = saved["id"]
            _, result, _ = request(f"/api/commands/{command_id}/run", {})
            assert "SAVED_OK" in result["output"]
            saved["name"] = "updated"
            assert request("/api/commands", saved)[1]["name"] == "updated"
            assert request(f"/api/commands/{command_id}/delete", {})[0] == 200
            assert request(f"/api/commands/{command_id}/run", {})[0] == 404
            assert request("/api/settings", {"desktop_interval": 0})[0] == 422
            assert request("/api/settings", {"desktop_zoom": 2, "desktop_interval": 2})[0] == 200
            assert Store(root).snapshot().settings.desktop_zoom == 2
            assert request("/api/settings", {"round_mode": True})[0] == 422
            state = request("/api/state")[1]
            assert state["relay"]["url"] == "https://super.tkcloud.online:444"
            assert "token" not in state["relay"]
            assert request("/api/relay", {"url": "http://public.example"})[0] == 422
            assert request("/api/relay", {"token": "short"})[0] == 422
            assert request("/api/relay", {"enabled": False, "token": ""})[0] == 200
            _, shortcuts, _ = request("/api/shortcuts")
            assert len(shortcuts) == 1 and shortcuts[0]["name"] == "sample"
            assert request("/api/shortcuts/not-allowed/launch", {})[0] == 400
            status, volume, _ = request("/api/volume")
            if status == 409 and "没有默认音频输出设备" in volume.get("detail", ""):
                print("SKIP volume mutation: no default audio output device; verified explicit unavailable response.")
            else:
                assert status == 200 and 0 <= volume["level"] <= 100, volume
                _, same_volume, _ = request("/api/volume", volume)
                assert same_volume == volume
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
            created = psutil.Process(child.pid).create_time()
            _, processes, _ = request("/api/processes")
            entry = next(item for item in processes if item["pid"] == child.pid)
            assert request(f"/api/processes/{child.pid}/terminate", {"create_time": created + 1})[0] == 400
            assert child.poll() is None
            assert request("/api/processes/favorite", {"key": entry["favorite_key"], "favorite": True})[0] == 200
            _, processes, _ = request("/api/processes")
            assert processes[0]["favorite"] and next(item for item in processes if item["pid"] == child.pid)["favorite"]
            assert entry["favorite_key"] in Store(root).snapshot().favorites
            assert request(f"/api/processes/{child.pid}/terminate", {"create_time": created, "force": False})[0] == 200
            child.wait(timeout=5)
            assert request(f"/api/processes/{psutil.Process().pid}/terminate", {"create_time": psutil.Process().create_time()})[0] == 400
            assert request("/api/ppt/state", authenticated=False)[0] == 401
            status, ppt, _ = request("/api/ppt/state")
            assert status == 200 and isinstance(ppt["available"], bool)
            assert request("/api/ppt/action", {"action": "next"}, csrf=False)[0] == 403
            assert request("/api/ppt/action", {"action": "not-an-action"})[0] == 422
            assert request("/api/ppt/goto", {"slide": 0, "deck": "old"})[0] == 422
            if not ppt["available"]:
                assert ppt["slides"] == [] and ppt["message"]
                assert request("/api/ppt/action", {"action": "start_current"})[0] == 409
                assert request("/api/ppt/slides/1?deck=old")[0] == 409
                assert request("/api/ppt/goto", {"slide": 1, "deck": "old"})[0] == 409
                print("SKIP live PPT export/show: PowerPoint unavailable; verified real COM unavailable and protected API responses.")
            else:
                assert 1 <= ppt["current_slide"] <= len(ppt["slides"])
                status, image, _ = request(f'/api/ppt/slides/1?deck={ppt["deck_id"]}&size=thumb')
                assert status == 200 and Image.open(io.BytesIO(image)).width == 320
                assert request("/api/ppt/slides/1?deck=old")[0] == 409
            _, info, _ = request("/api/desktop/info")
            assert info["monitors"][1]["width"] > 0
            status, jpeg, headers = request("/api/desktop/screenshot?monitor=1&width=640&quality=60")
            if status == 409 and isinstance(jpeg, dict) and "桌面截图不可用" in jpeg.get("detail", ""):
                print("SKIP JPEG image: Windows cannot capture this display session; verified explicit unavailable response and monitor enumeration.")
            else:
                assert status == 200 and Image.open(io.BytesIO(jpeg)).size[0] <= 640
                assert int(headers["X-Desktop-Width"]) == info["monitors"][1]["width"]
            assert request("/api/desktop/click", {"x": -200000, "y": -200000})[0] == 400
            assert request("/api/desktop/key", {"keys": ["ctrl", "alt", "delete"]})[0] == 400
            assert request("/api/desktop/key", {"keys": ["unsupported-key"]})[0] == 400
            app.state.store.update(web_paused=True)
            assert request("/api/state")[0] == 503 and request("/r")[0] == 503
            app.state.store.update(web_paused=False)
            assert request("/api/state")[0] == 200
            _, logs, _ = request("/api/logs")
            assert any("shell.finish" in line for line in logs["lines"])
            old_cookie = next(cookie for cookie in cookies if cookie.name == COOKIE)
            other_cookies = http.cookiejar.CookieJar()
            other_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(other_cookies))
            other_client.open(base + "/r?p=114514").close()
            assert next(cookie.value for cookie in other_cookies if cookie.name == COOKIE) != old_cookie.value
            assert request("/api/logout", {})[0] == 200
            assert request("/api/state")[0] == 401
            cookies.set_cookie(old_cookie)
            assert request("/api/state")[0] == 401, "Logged-out tokens must be revoked server-side"
            assert other_client.open(base + "/api/state").status == 200
            request("/r?p=114514")
            marker = root / "active-shell.pid"
            code = f"from pathlib import Path; import os,time; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
            command = subprocess.list2cmdline([sys.executable, "-c", code])
            completion = []
            def execute_active():
                completion.append(request("/api/shell", {"command": command, "timeout": 120}))
            active_thread = threading.Thread(target=execute_active, daemon=True)
            active_thread.start()
            deadline = time.monotonic() + 10
            while not marker.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Active command failed to start")
                threading.Event().wait(0.02)
            shell_child_pid = int(marker.read_text())
            app.state.shell.shutdown()
            active_thread.join(timeout=10)
            assert not active_thread.is_alive() and completion[0][0] == 200
            assert not psutil.pid_exists(shell_child_pid), "Shutdown must terminate active command descendants"
            assert request("/api/shell", {"command": "echo MUST_NOT_START"})[0] == 409
            print("PASS: real HTTP authentication/session revocation/CSRF/chunk limits, CMD+PowerShell+timeout+output bound, command CRUD, atomic settings, shortcut whitelist, process identity/favorites/termination, desktop metadata/input boundaries, pause/resume, logs, active command-tree shutdown. Hardware checks report their result above.")
        finally:
            app.state.shell.shutdown()
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            for handler in list(app.state.logger.handlers):
                handler.close()
                app.state.logger.removeHandler(handler)


if __name__ == "__main__":
    check()
