"""Check the real ordinary-user PPT module and its authenticated loopback boundary.

Run from an interactive Windows desktop: .venv/Scripts/python.exe ppt_helper_check.py
The test never changes an open presentation or sends keyboard input.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import tempfile
import time
import urllib.error
import secrets
import threading
import urllib.request

import psutil

from ppt_bridge import PowerPointBridge


def check(root: Path):
    bridge = PowerPointBridge(root, logging.getLogger("ppt-helper-check"))
    bridge.start()
    try:
        record = bridge._record()
        health = bridge._request("/health")
        assert isinstance(health, dict) and health["module_connected"] and health["module_elevated"] is False
        assert record["pid"] == health["module_pid"] != psutil.Process().pid
        process = psutil.Process(record["pid"])
        listeners = [connection for connection in process.net_connections(kind="tcp") if connection.status == psutil.CONN_LISTEN]
        assert len(listeners) == 1 and listeners[0].laddr.ip == "127.0.0.1"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, body=None, *, token=record["token"], headers=None):
            fields = {"Authorization": "Bearer " + token, **(headers or {})}
            if body is not None:
                fields["Content-Type"] = "application/json"
            req = urllib.request.Request(f'http://127.0.0.1:{record["port"]}' + path, data=None if body is None else json.dumps(body).encode(), headers=fields)
            try:
                response = opener.open(req, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, response.read()

        assert request("/health", token="wrong-token")[0] == 401
        assert request("/health", headers={"Origin": "http://attacker.example"})[0] == 403
        assert request("/health", headers={"Host": "attacker.example"})[0] == 400
        assert request("/action", {"action": "send_key"})[0] == 400
        assert request("/goto", {"slide": True, "deck": "old"})[0] == 400
        assert request("/shutdown", {"unexpected": True})[0] == 400
        assert request("/unknown")[0] == 404
        state = bridge.state()
        assert isinstance(state, dict)
        assert isinstance(state["available"], bool) and state["module_connected"] is True
        if not state["available"]:
            assert state["message"] and not state["slides"]
            try:
                bridge.action("start_current")
                raise AssertionError("Missing PowerPoint should reject operations")
            except RuntimeError:
                pass
        else:
            assert state["slides"]
            image = bridge.image(1, state["deck_id"], "thumb", "check")
            assert image.startswith(b"\xff\xd8")
        print("PASS: separate non-elevated helper PID, loopback-only listener, token/Origin/Host/schema boundary, real COM availability, no keyboard fallback.")
    finally:
        bridge.close()
    deadline = time.monotonic() + 10
    while (root / ".ppt-helper.json").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Helper did not clean its runtime record after shutdown")
        time.sleep(0.05)
    process.wait(timeout=5)
    print("PASS: authenticated helper shutdown, process exit and runtime-token cleanup.")


def check_saturated_shutdown():
    """Simulate eight blocked COM calls; health and shutdown must remain reachable."""
    from ppt_helper import HelperServer

    class BlockedCOM:
        def __init__(self):
            self.entered = 0
            self.condition = threading.Condition()
            self.release = threading.Event()

        def state(self):
            with self.condition:
                self.entered += 1
                self.condition.notify_all()
            if not self.release.wait(timeout=10):
                raise RuntimeError("COM test gate timed out")
            return {"available": False, "message": "Blocked COM test", "slides": []}

    controller = BlockedCOM()
    token = secrets.token_urlsafe(48)
    server = HelperServer(controller, token)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path, headers=headers, data=body)
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            response.read()
            return response.status

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(request, "/state") for _ in range(8)]
            try:
                with controller.condition:
                    assert controller.condition.wait_for(lambda: controller.entered == 8, timeout=5)
                assert request("/state") == 503
                assert request("/health") == 200
                assert request("/shutdown", b"{}") == 200
                worker.join(timeout=3)
                assert not worker.is_alive(), "Shutdown must not wait for occupied COM workers"
            finally:
                controller.release.set()
            assert all(future.result(timeout=5) == 200 for future in futures)
    finally:
        controller.release.set()
        if worker.is_alive():
            server.stop()
            worker.join(timeout=3)
        server.server_close()
    print("PASS: real helper HTTP health/shutdown stay reachable with eight simulated blocked COM calls.")


def check_start_close_race():
    """A late helper launch must still receive shutdown before the bridge closes."""
    from unittest.mock import patch
    from ppt_helper import HelperServer
    import os

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    with tempfile.TemporaryDirectory(prefix="ppt-close-race-") as directory:
        root = Path(directory)
        server = HelperServer(None, secrets.token_urlsafe(48))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        bridge = PowerPointBridge(root, logging.getLogger("ppt-close-race"))

        def delayed_launch(runtime):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Launch test gate timed out")
            (runtime / ".ppt-helper.json").write_text(json.dumps({"pid": os.getpid(), "port": server.server_port, "token": server.token}), encoding="utf-8")

        def close():
            bridge.close()
            closed.set()

        try:
            with patch("ppt_bridge.start_helper", side_effect=delayed_launch):
                starting = threading.Thread(target=bridge.start)
                starting.start()
                assert entered.wait(timeout=3)
                stopping = threading.Thread(target=close)
                stopping.start()
                assert not closed.wait(timeout=0.1), "Close must account for an in-progress launch"
                release.set()
                starting.join(timeout=5)
                stopping.join(timeout=5)
                worker.join(timeout=3)
                assert closed.is_set() and not worker.is_alive()
                bridge.start()
                assert not worker.is_alive(), "Closed bridges must not launch another helper"
        finally:
            release.set()
            if worker.is_alive():
                server.stop()
                worker.join(timeout=3)
            server.server_close()
    print("PASS: delayed module launch is accounted for during bridge shutdown; no orphan listener.")


if __name__ == "__main__":
    check_saturated_shutdown()
    check_start_close_race()
    with tempfile.TemporaryDirectory(prefix="remote-helper-module-check-") as directory:
        check(Path(directory))
