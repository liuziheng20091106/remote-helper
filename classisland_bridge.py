"""Same-user Windows named-pipe client for the ClassIsland 2.1 plugin."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import threading

MISSING = "ClassIsland 2.1 插件未连接，请安装 Remote Helper 插件并启动 ClassIsland。"
MAX_FRAME = 256 * 1024


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class ClassIslandBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        signatures = {
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
            "ProcessIdToSessionId": ([wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "WaitNamedPipeW": ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "GetOverlappedResult": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CancelIoEx": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        self._advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self._advapi.OpenProcessToken.restype = wintypes.BOOL
        self._advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self._advapi.GetTokenInformation.restype = wintypes.BOOL
        self._advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self._advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        self._require(self._advapi.OpenProcessToken(self._kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        sid = wintypes.LPWSTR()
        try:
            size = wintypes.DWORD()
            self._advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            self._require(self._advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
            self._require(self._advapi.ConvertSidToStringSidW(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.byref(sid)))
            session = wintypes.DWORD()
            self._require(self._kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)))
            self._pipe = rf"\\.\pipe\RemoteHelper.ClassIsland21.{sid.value}.{session.value}"
        finally:
            if sid:
                self._kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
            self._kernel.CloseHandle(token)

    @staticmethod
    def _require(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def _io(self, handle, buffer, count, *, write=False):
        event = self._require(self._kernel.CreateEventW(None, True, False, None))
        overlapped = _Overlapped(hEvent=event)
        transferred = wintypes.DWORD()
        try:
            function = self._kernel.WriteFile if write else self._kernel.ReadFile
            if not function(handle, buffer, count, ctypes.byref(transferred), ctypes.byref(overlapped)):
                if ctypes.get_last_error() != 997:
                    raise ctypes.WinError(ctypes.get_last_error())
                if self._kernel.WaitForSingleObject(event, 5000) != 0:
                    self._kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                    self._kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), True)
                    raise RuntimeError("ClassIsland 操作超时，请刷新状态确认操作结果；不会自动重试。")
                self._require(self._kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False))
            return transferred.value
        finally:
            self._kernel.CloseHandle(event)

    def request(self, action, **values):
        frame = json.dumps({"action": action, **values}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(frame) > 16384:
            raise ValueError("ClassIsland 请求过大。")
        with self._lock:
            if self._closed:
                raise RuntimeError("ClassIsland 连接已关闭。")
            handle = None
            try:
                self._require(self._kernel.WaitNamedPipeW(self._pipe, 1000))
                # Identification-only QoS prevents a rogue pipe impersonating this administrator.
                handle = self._kernel.CreateFileW(self._pipe, 0xC0000000, 0, None, 3, 0x40000000 | 0x00100000 | 0x00010000, None)
                if handle == ctypes.c_void_p(-1).value:
                    handle = None
                    raise ctypes.WinError(ctypes.get_last_error())
                outgoing = ctypes.create_string_buffer(frame)
                if self._io(handle, outgoing, len(frame), write=True) != len(frame):
                    raise RuntimeError("ClassIsland 请求发送中断，请刷新确认结果。")
                data = bytearray()
                while not data.endswith(b"\n"):
                    incoming = ctypes.create_string_buffer(8192)
                    size = self._io(handle, incoming, len(incoming))
                    if not size:
                        raise RuntimeError("ClassIsland 响应中断。")
                    data.extend(incoming.raw[:size])
                    if len(data) > MAX_FRAME:
                        raise RuntimeError("ClassIsland 响应超过大小限制。")
                response = json.loads(data)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise RuntimeError("ClassIsland 响应格式无效。")
                if not response["ok"]:
                    error = ValueError if response.get("kind") == "invalid" else RuntimeError
                    raise error(str(response.get("error") or "ClassIsland 操作失败。"))
                if not isinstance(response.get("data"), dict):
                    raise RuntimeError("ClassIsland 响应数据无效。")
                return response["data"]
            except (OSError, json.JSONDecodeError):
                raise RuntimeError(MISSING) from None
            finally:
                if handle is not None:
                    self._kernel.CloseHandle(handle)

    def state(self):
        try:
            return self.request("state")
        except RuntimeError as error:
            return {"connected": False, "message": str(error)}

    def close(self):
        with self._lock:
            self._closed = True
