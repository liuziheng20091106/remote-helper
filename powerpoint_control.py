"""Control the running Microsoft PowerPoint instance on one dedicated COM STA."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
import ctypes
import gc
import hashlib
import io
import math
import os
from pathlib import Path
from queue import Queue
import secrets
import tempfile
import threading
import time
from typing import Any

import comtypes
from comtypes.automation import IDispatch
from comtypes.client import GetActiveObject
from comtypes.client.dynamic import Dispatch
from PIL import Image


class PowerPointController:
    _ACTIONS = frozenset({"previous", "next", "start", "start_current", "end",
                          "first", "last", "black", "white"})
    _CACHE_SECONDS = 30
    _CACHE_BYTES = 32 * 1024 * 1024
    _CACHE_ENTRIES = 128

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._temporary = tempfile.TemporaryDirectory(prefix="remote-helper-ppt-")
        self._export_path = Path(self._temporary.name) / "slide.jpg"
        self._session = secrets.token_hex(16)
        self._presentation_identity = None
        self._live_token = ""
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_deck = ""
        self._revision = ""
        self._queue = Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._ready = Future()
        self._thread = threading.Thread(target=self._worker, name="PowerPoint STA", daemon=True)
        self._thread.start()
        error = self._ready.result()
        if error:
            self.close()
            raise RuntimeError(error)

    @staticmethod
    def _com_message(error):
        code = getattr(error, "hresult", None)
        if code is None:
            code = getattr(error, "winerror", None)
        if code is None and error.args and isinstance(error.args[0], int):
            code = error.args[0]
        code = code & 0xFFFFFFFF if isinstance(code, int) else None
        if code in {0x80040154, 0x800401F3}:
            return "未检测到 Microsoft PowerPoint，请先安装并打开 PowerPoint。"
        if code == 0x800401E3:
            return "Microsoft PowerPoint 尚未运行，请先打开演示文稿。"
        if code in {0x80010001, 0x8001010A}:
            return "PowerPoint 正忙或正在显示对话框，请关闭对话框后重试。"
        if code in {0x80010108, 0x800706BA}:
            return "PowerPoint 已关闭或连接中断，请重新打开演示文稿。"
        suffix = f"（HRESULT 0x{code:08X}）" if code is not None else ""
        return f"无法操作 Microsoft PowerPoint{suffix}，请确认演示文稿已打开且没有阻塞对话框。"

    def _worker(self):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception as error:
            self._ready.set_result(self._com_message(error))
            return
        self._ready.set_result(None)
        try:
            while True:
                request = self._queue.get()
                if request is None:
                    break
                operation, args, future = request
                try:
                    result = ("ok", getattr(self, "_" + operation)(*args))
                except ValueError as error:
                    result = ("value", str(error))
                except RuntimeError as error:
                    result = ("runtime", str(error))
                except (comtypes.COMError, OSError) as error:
                    result = ("runtime", self._com_message(error))
                except Exception as error:
                    result = ("runtime", f"PowerPoint 操作失败（{type(error).__name__}），请重试。")
                # Exceptions/tracebacks could own COM proxies: only plain data crosses threads.
                future.set_result(result)
        finally:
            self._presentation_identity = None
            gc.collect()
            comtypes.CoUninitialize()

    def _submit(self, operation, *args):
        with self._lock:
            if self._closed:
                raise RuntimeError("PowerPoint 控制器已关闭。")
            future = Future()
            self._queue.put((operation, args, future))
        kind, result = future.result()
        if kind == "value":
            raise ValueError(result)
        if kind == "runtime":
            raise RuntimeError(result)
        return result

    def state(self):
        return self._submit("state")

    def image(self, slide: int, deck: str, size: str, refresh=False):
        return self._submit("image", slide, deck, size, refresh)

    def action(self, action: str):
        return self._submit("action", action)

    def goto(self, slide: int, deck: str):
        return self._submit("goto", slide, deck)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._thread.join()
        with self._lock:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self._cache.clear()
            self._cache_bytes = 0

    @staticmethod
    def _context() -> tuple[Any, Any, Any]:
        # Supplying IDispatch avoids generating/importing an Office type library.
        app: Any = Dispatch(GetActiveObject("PowerPoint.Application", interface=IDispatch))
        shows = app.SlideShowWindows
        if int(shows.Count):
            show = shows.Item(1)
            return app, show.Presentation, show
        if not int(app.Presentations.Count):
            raise RuntimeError("PowerPoint 当前没有打开演示文稿。")
        return app, app.ActivePresentation, None

    def _deck_id(self, presentation):
        # Keep IUnknown alive on this STA so a closed deck cannot reuse its identity.
        identity = presentation._comobj.QueryInterface(comtypes.IUnknown)
        address = ctypes.cast(identity, ctypes.c_void_p).value
        previous = ctypes.cast(self._presentation_identity, ctypes.c_void_p).value if self._presentation_identity is not None else None
        if address != previous:
            self._presentation_identity = identity
            self._live_token = secrets.token_hex(24)
        return self._live_token

    def _bookmark_id(self, presentation, show):
        if str(presentation.Path or ""):
            identity = "file\0" + os.path.normcase(str(presentation.FullName)).casefold()
        else:
            windows = presentation.Windows
            hwnd = int(windows.Item(1).HWND) if int(windows.Count) else int(show.HWND) if show is not None else 0
            identity = f"unsaved\0{self._session}\0{hwnd}\0{presentation.Name}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _dimensions(presentation):
        setup = presentation.PageSetup
        width, height = float(setup.SlideWidth), float(setup.SlideHeight)
        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
            raise RuntimeError("无法读取演示文稿的幻灯片尺寸。")
        return width, height

    @staticmethod
    def _current_slide(app, presentation, show):
        count = int(presentation.Slides.Count)
        if not count:
            return 0
        if show is not None:
            return max(1, min(count, int(show.View.Slide.SlideIndex)))
        window = app.ActiveWindow
        try:
            selection = window.Selection
            if int(selection.Type) == 1:  # ppSelectionSlides (including slide sorter).
                return max(1, min(count, int(selection.SlideRange.Item(1).SlideIndex)))
        except (comtypes.COMError, AttributeError):
            pass
        try:
            return max(1, min(count, int(window.View.Slide.SlideIndex)))
        except (comtypes.COMError, AttributeError):
            try:
                return max(1, min(count, int(window.Selection.SlideRange.Item(1).SlideIndex)))
            except (comtypes.COMError, AttributeError):
                return 1

    @staticmethod
    def _title(slide):
        try:
            shapes = slide.Shapes
            if int(shapes.HasTitle):
                return str(shapes.Title.TextFrame.TextRange.Text or "").strip()
        except (comtypes.COMError, AttributeError):
            pass
        return ""

    @staticmethod
    def _unavailable(message):
        return {"available": False, "message": message, "name": "", "deck_id": "", "bookmark_id": "",
                "current_slide": 0, "slideshow": False, "width": 0.0, "height": 0.0,
                "slides": []}

    def _state(self):
        try:
            app, presentation, show = self._context()
            width, height = self._dimensions(presentation)
            slides = presentation.Slides
            # ponytail: live metadata costs O(slides); add event invalidation only for slow large decks.
            metadata = []
            for index in range(1, int(slides.Count) + 1):
                slide = slides.Item(index)
                metadata.append({"index": index, "id": int(slide.SlideID), "title": self._title(slide)})
            return {"available": True, "message": "", "name": str(presentation.Name),
                    "deck_id": self._deck_id(presentation),
                    "bookmark_id": self._bookmark_id(presentation, show),
                    "current_slide": self._current_slide(app, presentation, show),
                    "slideshow": show is not None, "width": width, "height": height,
                    "slides": metadata}
        except RuntimeError as error:
            return self._unavailable(str(error))
        except (comtypes.COMError, OSError) as error:
            return self._unavailable(self._com_message(error))

    def _target(self, slide, deck):
        if isinstance(slide, bool) or not isinstance(slide, int):
            raise ValueError("幻灯片页码必须是整数。")
        if not isinstance(deck, str) or not deck:
            raise ValueError("缺少演示文稿标识，请刷新页面。")
        app, presentation, show = self._context()
        if deck != self._deck_id(presentation):
            raise RuntimeError("当前演示文稿已切换，请刷新幻灯片列表后重试。")
        if not 1 <= slide <= int(presentation.Slides.Count):
            raise ValueError("幻灯片页码超出当前演示文稿范围。")
        return app, presentation, show

    def _clear_cache(self):
        self._cache.clear()
        self._cache_bytes = 0

    def _image(self, slide, deck, size, refresh):
        if not isinstance(size, str) or size not in {"thumb", "large"}:
            raise ValueError("图片尺寸必须为 thumb 或 large。")
        if not isinstance(refresh, (str, bool)):
            raise ValueError("图片刷新标识必须为字符串或布尔值。")
        app, presentation, show = self._target(slide, deck)
        if deck != self._cache_deck:
            self._clear_cache()
            self._cache_deck, self._revision = deck, ""
        if isinstance(refresh, str) and refresh and refresh != self._revision:
            self._clear_cache()
            self._revision = refresh
        width = 320 if size == "thumb" else 1600
        slide_width, slide_height = self._dimensions(presentation)
        height = max(1, round(width * slide_height / slide_width))
        selected = presentation.Slides.Item(slide)
        key = (int(selected.SlideID), width, height)
        cached = self._cache.pop(key, None)
        if cached is not None:
            timestamp, data = cached
            self._cache_bytes -= len(data)
            if refresh is not True and time.monotonic() - timestamp < self._CACHE_SECONDS:
                self._cache[key] = cached
                self._cache_bytes += len(data)
                return data
        try:
            self._export_path.unlink(missing_ok=True)
            selected.Export(str(self._export_path), "JPG", width, height)
            with Image.open(self._export_path) as exported:
                if exported.format == "JPEG" and exported.size == (width, height):
                    data = self._export_path.read_bytes()
                else:
                    with exported.convert("RGB") as rgb:
                        with rgb.resize((width, height), Image.Resampling.LANCZOS) as resized:
                            output = io.BytesIO()
                            resized.save(output, format="JPEG", quality=85)
                            data = output.getvalue()
        finally:
            self._export_path.unlink(missing_ok=True)
        if len(data) <= self._CACHE_BYTES:
            while self._cache and (len(self._cache) >= self._CACHE_ENTRIES or
                                   self._cache_bytes + len(data) > self._CACHE_BYTES):
                _, (_, previous) = self._cache.popitem(last=False)
                self._cache_bytes -= len(previous)
            self._cache[key] = (time.monotonic(), data)
            self._cache_bytes += len(data)
        return data

    @staticmethod
    def _goto_slide(app, presentation, show, slide):
        if show is not None:
            show.View.GotoSlide(slide)
            return
        try:
            app.ActiveWindow.View.GotoSlide(slide)
        except comtypes.COMError:
            # Slide sorter/outline views can expose selection instead of slide navigation.
            presentation.Slides.Item(slide).Select()

    def _goto(self, slide, deck):
        app, presentation, show = self._target(slide, deck)
        self._goto_slide(app, presentation, show, slide)
        return self._state()

    def _action(self, action):
        if not isinstance(action, str) or action not in self._ACTIONS:
            raise ValueError("不支持的 PowerPoint 操作。")
        app, presentation, show = self._context()
        count = int(presentation.Slides.Count)
        if not count:
            raise RuntimeError("当前演示文稿没有可控制的幻灯片。")
        if action == "end":
            if show is not None:
                show.View.Exit()
        elif action in {"black", "white"}:
            if show is None:
                raise RuntimeError("请先开始幻灯片放映，再切换黑屏或白屏。")
            state = 3 if action == "black" else 4  # ppSlideShowBlackScreen / WhiteScreen.
            view = show.View
            view.State = 1 if int(view.State) == state else state  # ppSlideShowRunning.
        elif action in {"start", "start_current"}:
            if show is not None:
                if action == "start":
                    show.View.GotoSlide(1)
            else:
                expected = presentation._comobj.QueryInterface(comtypes.IUnknown)
                current = self._current_slide(app, presentation, None)
                app.ActiveWindow.Activate()
                # Native F5/Shift+F5 commands do not persist changed SlideShowSettings.
                command = "SlideShowFromBeginning" if action == "start" else "SlideShowFromCurrent"
                bars = app.CommandBars
                if not bars.GetEnabledMso(command):
                    raise RuntimeError("PowerPoint 当前无法开始放映，请关闭对话框并切换到幻灯片编辑窗口。")
                bars.ExecuteMso(command)
                shows = app.SlideShowWindows
                for index in range(1, int(shows.Count) + 1):
                    started = shows.Item(index)
                    identity = started.Presentation._comobj.QueryInterface(comtypes.IUnknown)
                    if ctypes.cast(identity, ctypes.c_void_p).value == ctypes.cast(expected, ctypes.c_void_p).value:
                        if action == "start_current" and int(started.View.Slide.SlideIndex) != current:
                            started.View.GotoSlide(current)
                        break
                else:
                    raise RuntimeError("PowerPoint 未进入幻灯片放映，请检查 PowerPoint 窗口后重试。")
        else:
            current = self._current_slide(app, presentation, show)
            target = {"first": 1, "last": count, "previous": max(1, current - 1),
                      "next": min(count, current + 1)}[action]
            if target != current:
                self._goto_slide(app, presentation, show, target)
        return self._state()
