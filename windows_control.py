import ctypes
from ctypes import wintypes
import hashlib
import io
import math
import os
from pathlib import Path
import threading

# Establish physical desktop coordinates before importing input/capture libraries.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except AttributeError:
    _user32.SetProcessDPIAware()

import comtypes
import mss
from PIL import Image
import psutil
import pyautogui
from pycaw.pycaw import AudioUtilities


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _InputUnion)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
try:
    _user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
except AttributeError:
    pass


class WindowsController:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._input_lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._processes = {}
        self._keys = frozenset(key for key in pyautogui.KEYBOARD_KEYS
                               if pyautogui.platformModule.keyboardMapping.get(key) is not None)
        # Remote control must remain usable when the cursor reaches a screen corner.
        pyautogui.FAILSAFE = False

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    @staticmethod
    def _dpi_context():
        try:
            previous = _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not previous:
                raise ctypes.WinError(ctypes.get_last_error())
            return previous
        except AttributeError:
            return None

    @staticmethod
    def _restore_dpi(previous):
        if previous is not None:
            _user32.SetThreadDpiAwarenessContext(previous)

    def shortcuts(self):
        directory = self.root / "Fix Tools"
        if not directory.is_dir():
            return []
        allowed_root = directory.resolve()
        result = []
        for path in directory.rglob("*"):
            if path.suffix.lower() not in {".lnk", ".url"} or not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(allowed_root):
                continue
            relative = path.relative_to(directory).as_posix()
            shortcut_id = hashlib.sha256(relative.casefold().encode("utf-8")).hexdigest()
            result.append({"id": shortcut_id, "name": str(Path(relative).with_suffix("")),
                           "path": str(resolved)})
        return sorted(result, key=lambda item: item["name"].casefold())

    def launch_shortcut(self, shortcut_id):
        if not isinstance(shortcut_id, str):
            raise ValueError("Invalid shortcut ID.")
        for shortcut in self.shortcuts():
            if shortcut["id"] == shortcut_id:
                os.startfile(shortcut["path"])
                return {"ok": True, "name": shortcut["name"], "path": shortcut["path"]}
        raise ValueError("Shortcut no longer exists or is not allowed.")

    def volume(self, level=None, muted=None):
        if level is not None:
            if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or not 0 <= level <= 100:
                raise ValueError("Volume level must be from 0 to 100.")
        if muted is not None and not isinstance(muted, bool):
            raise ValueError("muted must be a boolean.")
        comtypes.CoInitialize()
        speakers = endpoint = None
        try:
            try:
                speakers = AudioUtilities.GetSpeakers()
            except comtypes.COMError as error:
                if error.hresult == -2147023728:
                    raise RuntimeError("当前没有默认音频输出设备，请连接扬声器或耳机后重试。") from error
                raise
            if speakers is None:
                raise RuntimeError("当前没有默认音频输出设备，请连接扬声器或耳机后重试。")
            endpoint = speakers.EndpointVolume
            if level is not None:
                endpoint.SetMasterVolumeLevelScalar(float(level) / 100, None)
            if muted is not None:
                endpoint.SetMute(int(muted), None)
            return {"level": round(endpoint.GetMasterVolumeLevelScalar() * 100, 1),
                    "muted": bool(endpoint.GetMute())}
        finally:
            # Clear every owning reference before leaving this COM apartment.
            endpoint = speakers = None
            comtypes.CoUninitialize()

    def processes(self, favorites):
        favorite_keys = set(favorites)
        result = []
        with self._process_lock:
            alive = {}
            for process in psutil.process_iter():
                try:
                    with process.oneshot():
                        def get_value(getter, default):
                            try:
                                return getter()
                            except (psutil.AccessDenied, psutil.ZombieProcess):
                                return default
                        name = get_value(process.name, "Unknown") or "Unknown"
                        exe = get_value(process.exe, "") or ""
                        username = get_value(process.username, "") or ""
                        created = get_value(process.create_time, None)
                        identity = (process.pid, created)
                        # Retain Process instances so cpu_percent measures successive polls.
                        sampled = self._processes.get(identity, process)
                        cpu = get_value(sampled.cpu_percent, 0.0)
                        memory = get_value(process.memory_info, None)
                        favorite_key = os.path.normcase(os.path.normpath(exe)) if exe else name.casefold()
                        alive[identity] = sampled
                        result.append({"pid": process.pid, "name": name, "username": username,
                                       "exe": exe, "memory_mb": round(memory.rss / (1024 * 1024), 1) if memory else 0.0,
                                       "cpu_percent": round(cpu, 1), "create_time": created,
                                       "favorite_key": favorite_key, "favorite": favorite_key in favorite_keys})
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
            self._processes = alive
        return sorted(result, key=lambda item: (not item["favorite"], -item["memory_mb"], item["name"].casefold(), item["pid"]))

    def terminate(self, pid, create_time, force=False):
        self._integer(pid, "pid", 0, 2 ** 32 - 1)
        if isinstance(create_time, bool) or not isinstance(create_time, (int, float)) or not math.isfinite(create_time) or create_time <= 0:
            raise ValueError("A valid process creation time is required.")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean.")
        if pid in {0, 4, os.getpid(), os.getppid()}:
            raise ValueError("This system or Remote Helper process is protected.")
        try:
            process = psutil.Process(pid)
            if process.create_time() != create_time:
                raise ValueError("The process has changed; refresh the process list.")
            if process.name().casefold() in {"system", "system idle process", "idle"}:
                raise ValueError("This system process is protected.")
            if force:
                process.kill()
            else:
                process.terminate()
            try:
                process.wait(timeout=2)
            except psutil.TimeoutExpired as error:
                raise RuntimeError("Termination was requested, but the process is still running.") from error
            return {"ok": True}
        except psutil.NoSuchProcess as error:
            raise ValueError("The process no longer exists.") from error
        except psutil.AccessDenied as error:
            raise PermissionError("Windows denied permission to terminate this process.") from error

    @staticmethod
    def _monitors(capture):
        monitors = capture.monitors
        physical = sorted(monitors[1:], key=lambda rect: (rect["left"] != 0 or rect["top"] != 0))
        return [monitors[0], *physical]

    def desktop_info(self):
        previous = self._dpi_context()
        try:
            with mss.mss() as capture:
                monitors = [{"id": index, "left": monitor["left"], "top": monitor["top"],
                             "width": monitor["width"], "height": monitor["height"],
                             "name": "All monitors" if index == 0 else ("Primary monitor" if index == 1 else f"Monitor {index}")}
                            for index, monitor in enumerate(self._monitors(capture))]
            return {"monitors": monitors, "keys": sorted(self._keys)}
        finally:
            self._restore_dpi(previous)

    def screenshot(self, monitor=1, width=1280, quality=65):
        self._integer(monitor, "monitor", 0, 128)
        self._integer(width, "width", 320, 2560)
        self._integer(quality, "quality", 30, 90)
        previous = self._dpi_context()
        try:
            with mss.mss() as capture:
                monitors = self._monitors(capture)
                if monitor >= len(monitors):
                    raise ValueError("Monitor does not exist.")
                geometry = monitors[monitor]
                shot = capture.grab(geometry)
                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality)
            return output.getvalue(), {"left": geometry["left"], "top": geometry["top"],
                                       "width": geometry["width"], "height": geometry["height"],
                                       "image_width": image.width, "image_height": image.height}
        except mss.exception.ScreenShotError as error:
            raise RuntimeError("桌面截图不可用，请保持 Windows 用户已登录且桌面显示会话可用。系统错误：" + str(error)) from error
        finally:
            self._restore_dpi(previous)

    def click(self, x, y, button="left", clicks=1):
        self._integer(x, "x", -(2 ** 31), 2 ** 31 - 1)
        self._integer(y, "y", -(2 ** 31), 2 ** 31 - 1)
        self._integer(clicks, "clicks", 1, 2)
        if button not in {"left", "right", "middle"}:
            raise ValueError("button must be left, right, or middle.")
        with self._input_lock:
            previous = self._dpi_context()
            try:
                with mss.mss() as capture:
                    if not any(rect["left"] <= x < rect["left"] + rect["width"] and
                               rect["top"] <= y < rect["top"] + rect["height"]
                               for rect in capture.monitors[1:]):
                        raise ValueError("Click is outside the physical monitors.")
                pyautogui.click(x=x, y=y, clicks=clicks, interval=0.1 if clicks == 2 else 0, button=button)
            finally:
                self._restore_dpi(previous)
        return {"ok": True}

    def key(self, keys):
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8 or any(not isinstance(key, str) for key in keys):
            raise ValueError("keys must contain between 1 and 8 key names.")
        keys = [key.lower() for key in keys]
        if any(key not in self._keys for key in keys):
            raise ValueError("An unsupported key was requested.")
        if any(key in {"ctrl", "ctrlleft", "ctrlright"} for key in keys) and any(key in {"alt", "altleft", "altright"} for key in keys) and any(key in {"delete", "del"} for key in keys):
            raise ValueError("Ctrl+Alt+Delete cannot be sent remotely.")
        with self._input_lock:
            pressed = []
            try:
                for key in keys:
                    pyautogui.keyDown(key)
                    pressed.append(key)
            finally:
                for key in reversed(pressed):
                    pyautogui.keyUp(key)
        return {"ok": True}

    def text(self, text):
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("text must be a string of at most 10000 characters.")
        try:
            encoded = text.encode("utf-16-le")
        except UnicodeEncodeError as error:
            raise ValueError("text contains an invalid Unicode surrogate.") from error
        if not encoded:
            return {"ok": True}
        count = len(encoded) // 2
        events = (_Input * (count * 2))()
        for index in range(count):
            unit = encoded[2 * index] | encoded[2 * index + 1] << 8
            events[2 * index].type = events[2 * index + 1].type = 1
            events[2 * index].ki = _KeyboardInput(0, unit, 0x0004, 0, 0)
            events[2 * index + 1].ki = _KeyboardInput(0, unit, 0x0004 | 0x0002, 0, 0)
        with self._input_lock:
            ctypes.set_last_error(0)
            sent = _user32.SendInput(len(events), events, ctypes.sizeof(_Input))
            if sent != len(events):
                error = ctypes.get_last_error()
                if sent % 2:
                    release = events[sent]
                    _user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(_Input))
                raise RuntimeError(f"Windows inserted {sent} of {len(events)} Unicode input events (error {error}); input may be blocked by a higher-privilege window.")
        return {"ok": True}

    def scroll(self, amount):
        self._integer(amount, "amount", -100, 100)
        with self._input_lock:
            # PyAutoGUI's pinned Windows backend expects raw WHEEL_DELTA units.
            pyautogui.scroll(amount * 120)
        return {"ok": True}
