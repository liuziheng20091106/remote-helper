"""Non-elevated, authenticated loopback transport for the PowerPoint COM STA."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
MAX_BODY = 128 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
ACTIONS = frozenset({"previous", "next", "start", "start_current", "end",
                     "first", "last", "black", "white"})
LOG = logging.getLogger("ppt-helper")


class HelperBusy(RuntimeError):
    pass


def _instance_mutex(root):
    """Refuse elevation and hold a root-specific mutex accessible by this user only."""
    if os.name != "nt":
        raise RuntimeError("PowerPoint 模块仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    kernel.CreateMutexW.argtypes = [ctypes.POINTER(SecurityAttributes), wintypes.BOOL,
                                    wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        elevated = wintypes.DWORD()
        length = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(elevated),
                                           ctypes.sizeof(elevated), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        if elevated.value:
            raise RuntimeError("PowerPoint 模块不能以管理员身份运行，请正常双击 Start-PPT.bat。")
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        user = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, user, len(user), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;GA;;;{sid_text.value})", 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, False)
        identity = os.path.normcase(str(root.resolve())).encode("utf-8")
        name = "Local\\RemoteHelperPPT-" + hashlib.sha256(identity).hexdigest()
        handle = kernel.CreateMutexW(ctypes.byref(attributes), False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS: never touch the existing instance's record.
            kernel.CloseHandle(handle)
            return None
        return kernel, handle
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)


class HelperServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True
    request_queue_size = 16

    def __init__(self, controller, token):
        self.controller = controller
        self.token = token
        self.slots = threading.BoundedSemaphore(16)
        # Reserve HTTP workers for health/shutdown while COM requests wait on the STA.
        self.operations = threading.BoundedSemaphore(8)
        self.shutdown_lock = threading.Lock()
        self.stopping = False
        super().__init__(("127.0.0.1", 0), HelperHandler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            body = b'{"detail":"PowerPoint helper is busy."}'
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Type: application/json\r\nConnection: close\r\n"
                                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def stop(self):
        with self.shutdown_lock:
            if not self.stopping:
                self.stopping = True
                threading.Thread(target=self.shutdown, name="PPT HTTP shutdown").start()

    def handle_error(self, request, client_address):
        # Never log raw requests, Authorization, or request parameters.
        LOG.error("HTTP worker failed")


class HelperHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PPT-Helper"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # Base implementation includes attacker-controlled paths and headers.

    def _respond(self, status, value, content_type="application/json; charset=utf-8"):
        if not isinstance(value, bytes):
            value = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(value) > MAX_RESPONSE:
            status = 500
            content_type = "application/json; charset=utf-8"
            value = b'{"detail":"PowerPoint response exceeds 8 MiB."}'
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(value)

    def send_error(self, code, message=None, explain=None):
        self._respond(code, {"detail": self.responses.get(code, ("Invalid request",))[0]})

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def handle_one_request(self):
        # BaseHTTPRequestHandler silently drops header timeouts; report them instead.
        self.requestline = ""
        self.request_version = "HTTP/1.1"
        self.command = ""
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self._dispatch()
            self.wfile.flush()
        except TimeoutError:
            self.send_error(408)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.command not in {"GET", "POST"}:
            self.send_error(405)
            return False
        return True

    def _authorize(self):
        if self.headers.get_all("Origin"):
            self.send_error(403)
            return False
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        if len(hosts) != 1 or hosts[0].lower() not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.send_error(400)
            return False
        values = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.token
        if len(values) != 1 or not secrets.compare_digest(values[0].encode("utf-8"), expected.encode("ascii")):
            self.send_error(401)
            return False
        return True

    def _body(self):
        if self.headers.get_all("Transfer-Encoding"):
            self.send_error(411)
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self.send_error(411)
            return None
        if len(lengths[0]) > 10 or int(lengths[0]) > MAX_BODY:
            self.send_error(413)
            return None
        length = int(lengths[0])
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
            self.send_error(415)
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete JSON body.")

        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field.")
                result[key] = value
            return result

        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON constant.")))
        except (UnicodeError, RecursionError) as error:
            raise ValueError("Invalid JSON body.") from error
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")
        return data

    @staticmethod
    def _target(slide, deck):
        if type(slide) is not int or not 1 <= slide <= 2147483647:
            raise ValueError("Slide must be an integer between 1 and 2147483647.")
        if not isinstance(deck, str) or not 1 <= len(deck) <= 256 or any(ord(c) < 32 for c in deck):
            raise ValueError("Invalid presentation identifier.")

    @staticmethod
    def _with_module(state):
        state.update(module_connected=True, module_pid=os.getpid(), module_elevated=False)
        return state

    def _operation(self, name, *args):
        if not self.server.operations.acquire(blocking=False):
            raise HelperBusy("PowerPoint 模块繁忙，请稍后重试。")
        try:
            return getattr(self.server.controller, name)(*args)
        finally:
            self.server.operations.release()

    def _dispatch(self):
        try:
            if not self._authorize():
                return
            if self.headers.get_all("Transfer-Encoding"):
                self.send_error(411)
                return
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                raise ValueError("Only local endpoint paths are accepted.")
            routes = {"/health": "GET", "/state": "GET", "/image": "GET", "/action": "POST",
                      "/goto": "POST", "/shutdown": "POST"}
            method = routes.get(target.path)
            if method is None:
                self.send_error(404)
                return
            if method != self.command:
                self.send_error(405)
                return
            if self.command == "GET":
                lengths = self.headers.get_all("Content-Length", [])
                if lengths and lengths != ["0"]:
                    raise ValueError("GET requests cannot have a body.")
                if target.path in {"/state", "/health"}:
                    if target.query:
                        raise ValueError("This endpoint accepts no parameters.")
                    state = self._operation("state") if target.path == "/state" else {}
                    self._respond(200, self._with_module(state))
                else:
                    params = parse_qs(target.query, keep_blank_values=True, strict_parsing=True,
                                      encoding="utf-8", errors="strict", max_num_fields=4)
                    if set(params) != {"slide", "deck", "size", "refresh"} or any(len(v) != 1 for v in params.values()):
                        raise ValueError("Image requires slide, deck, size and refresh exactly once.")
                    slide_text, deck = params["slide"][0], params["deck"][0]
                    if not slide_text.isascii() or not slide_text.isdecimal() or len(slide_text) > 10:
                        raise ValueError("Invalid slide number.")
                    slide = int(slide_text)
                    self._target(slide, deck)
                    size, refresh = params["size"][0], params["refresh"][0]
                    if size not in {"thumb", "large"} or len(refresh) > 256 or any(ord(c) < 32 for c in refresh):
                        raise ValueError("Invalid image size or refresh identifier.")
                    self._respond(200, self._operation("image", slide, deck, size, refresh), "image/jpeg")
                return
            if target.query:
                raise ValueError("POST endpoints accept no query parameters.")
            body = self._body()
            if body is None:
                return
            if target.path == "/action":
                if set(body) != {"action"} or not isinstance(body["action"], str) or body["action"] not in ACTIONS:
                    raise ValueError("Invalid PowerPoint action.")
                self._respond(200, self._with_module(self._operation("action", body["action"])))
            elif target.path == "/goto":
                if set(body) != {"slide", "deck"}:
                    raise ValueError("Goto requires slide and deck only.")
                self._target(body["slide"], body["deck"])
                self._respond(200, self._with_module(self._operation("goto", body["slide"], body["deck"])))
            else:
                if body:
                    raise ValueError("Shutdown requires an empty object.")
                self._respond(200, {"ok": True})
                self.server.stop()
        except (TimeoutError, socket.timeout):
            self.send_error(408)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ValueError as error:
            self._respond(400, {"detail": str(error)})
        except HelperBusy as error:
            self._respond(503, {"detail": str(error)})
        except RuntimeError as error:
            self._respond(409, {"detail": str(error)})
        except Exception as error:
            LOG.error("Request failed (%s)", type(error).__name__)
            self._respond(500, {"detail": "PowerPoint helper request failed."})


def main(root=ROOT):
    root = Path(root).resolve()
    mutex = _instance_mutex(root)
    if mutex is None:
        return 0
    server = controller = None
    metadata = root / ".ppt-helper.json"
    token = secrets.token_urlsafe(48)
    temporary = root / f".ppt-helper.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        (root / "logs").mkdir(exist_ok=True)
        handler = RotatingFileHandler(root / "logs" / "ppt-helper.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        from powerpoint_control import PowerPointController
        controller = PowerPointController(root)
        server = HelperServer(controller, token)
        record = {"pid": os.getpid(), "port": server.server_port, "token": token}
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, metadata)
        LOG.info("PowerPoint helper started (pid=%d)", os.getpid())
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        return 0
    except Exception as error:
        LOG.error("PowerPoint helper startup failed (%s)", type(error).__name__)
        raise
    finally:
        try:
            try:
                if server is not None:
                    server.server_close()
            finally:
                if controller is not None:
                    controller.close()  # COM release and CoUninitialize remain on its STA.
        finally:
            temporary.unlink(missing_ok=True)
            try:
                current = json.loads(metadata.read_text(encoding="utf-8"))
                if current.get("pid") == os.getpid() and current.get("token") == token:
                    metadata.unlink(missing_ok=True)
            except (OSError, ValueError, AttributeError):
                pass
            mutex[0].CloseHandle(mutex[1])
            LOG.info("PowerPoint helper stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    raise SystemExit(main(parser.parse_args().root))
