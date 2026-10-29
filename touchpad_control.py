"""Native Windows relative mouse and gesture input for the remote touchpad."""

from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from windows_control import _Input, _KeyboardInput, _MouseInput, _user32


_MOUSE_MOVE = 0x0001
_LEFT_DOWN, _LEFT_UP = 0x0002, 0x0004
_RIGHT_DOWN, _RIGHT_UP = 0x0008, 0x0010
_MIDDLE_DOWN, _MIDDLE_UP = 0x0020, 0x0040
_MOUSE_WHEEL, _MOUSE_HWHEEL = 0x0800, 0x01000
_KEY_UP = 0x0002

_BUTTONS = ("left", "middle", "right")
_BUTTON_FLAGS = {
    "left": (_LEFT_DOWN, _LEFT_UP),
    "right": (_RIGHT_DOWN, _RIGHT_UP),
    "middle": (_MIDDLE_DOWN, _MIDDLE_UP),
}
_VK = {"win": 0x5B, "tab": 0x09, "d": 0x44, "alt": 0x12, "shift": 0x10, "ctrl": 0x11}
_GESTURES = {
    "task_view": ("win", "tab"),
    "desktop": ("win", "d"),
    "previous_app": ("alt", "shift", "tab"),
    "next_app": ("alt", "tab"),
}


class _InputDispatchError(RuntimeError):
    def __init__(self, sent: int, total: int, error: int):
        self.sent = sent
        super().__init__(
            f"Windows 未能完成触控板输入（已发送 {sent}/{total}，错误 {error}）。"
            "请保持程序所在的 Windows 桌面已登录、解锁且会话连接；控制管理员窗口时需以管理员身份运行。"
        )


@dataclass
class _Owner:
    last_seq: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    held: set[str] = field(default_factory=set)
    keys: set[str] = field(default_factory=set)


class TouchpadController:
    """Serialize and validate all touchpad input before calling SendInput."""

    _MAX_OWNERS = 64
    _LEASE_SECONDS = 3.0
    _TOMBSTONE_SECONDS = 300.0

    def __init__(self, controller: Any):
        self._controller = controller
        self._owners: dict[str, _Owner] = {}
        self._active_owner: str | None = None
        self._closed = False
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                           name="touchpad-watchdog", daemon=True)
        self._watchdog.start()

    @staticmethod
    def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    @staticmethod
    def _strict_bool(value: Any, name: str) -> bool:
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean.")
        return value

    @classmethod
    def _validate_event(cls, event: Any) -> tuple:
        if type(event) is not dict or type(event.get("type")) is not str:
            raise ValueError("Each touchpad event must be an object with a string type.")
        kind = event["type"]
        allowed: dict[str, set[str]] = {
            "move": {"type", "dx", "dy"},
            "wheel": {"type", "dx", "dy"},
            "button": {"type", "button", "down"},
            "click": {"type", "button", "clicks"},
            "zoom": {"type", "steps"},
            "gesture": {"type", "name"},
        }
        if kind not in allowed or set(event) != allowed[kind]:
            raise ValueError(f"Invalid {kind!r} touchpad event.")
        if kind == "move":
            return kind, cls._integer(event["dx"], "dx", -4096, 4096), cls._integer(event["dy"], "dy", -4096, 4096)
        if kind == "wheel":
            return kind, cls._integer(event["dx"], "dx", -2400, 2400), cls._integer(event["dy"], "dy", -2400, 2400)
        if kind == "button":
            button = event["button"]
            if type(button) is not str or button not in _BUTTONS:
                raise ValueError("button must be left, right, or middle.")
            return kind, button, cls._strict_bool(event["down"], "down")
        if kind == "click":
            button = event["button"]
            if type(button) is not str or button not in _BUTTONS:
                raise ValueError("button must be left, right, or middle.")
            return kind, button, cls._integer(event["clicks"], "clicks", 1, 2)
        if kind == "zoom":
            return kind, cls._integer(event["steps"], "steps", -10, 10)
        name = event["name"]
        if type(name) is not str or name not in _GESTURES:
            raise ValueError("Unknown touchpad gesture.")
        return kind, name

    @classmethod
    def _validate_batch(cls, events: Any, release: Any) -> tuple[list[tuple], bool]:
        if type(events) is not list or len(events) > 64:
            raise ValueError("events must be a list of at most 64 objects.")
        return [cls._validate_event(event) for event in events], cls._strict_bool(release, "release")

    @staticmethod
    def _mouse(dx: int = 0, dy: int = 0, data: int = 0, flags: int = 0) -> _Input:
        item = _Input()
        item.type = 0
        item.mi = _MouseInput(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return item

    @staticmethod
    def _key(vk: int, up: bool = False) -> _Input:
        item = _Input()
        item.type = 1
        item.ki = _KeyboardInput(vk, 0, _KEY_UP if up else 0, 0, 0)
        return item

    @classmethod
    def _gesture_inputs(cls, name: str) -> list[_Input]:
        keys = _GESTURES[name]
        return [cls._key(_VK[key]) for key in keys] + [cls._key(_VK[key], True) for key in reversed(keys)]

    @staticmethod
    def _send(inputs: list[_Input]) -> None:
        if not inputs:
            return
        array = (_Input * len(inputs))(*inputs)
        ctypes.set_last_error(0)
        sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
        if sent != len(inputs):
            error = ctypes.get_last_error()
            raise _InputDispatchError(sent, len(inputs), error)

    @classmethod
    def _release_inputs(cls, buttons: set[str]) -> list[_Input]:
        return [cls._mouse(flags=_BUTTON_FLAGS[button][1]) for button in _BUTTONS if button in buttons]

    @classmethod
    def _release_keys(cls, keys: set[str]) -> list[_Input]:
        return [cls._key(_VK[key], True) for key in keys]

    def _release_owner_locked(self, owner: str, state: _Owner) -> None:
        if not state.held and not state.keys:
            if self._active_owner == owner:
                self._active_owner = None
            return
        self._send(self._release_inputs(state.held) + self._release_keys(state.keys))
        state.held.clear()
        state.keys.clear()
        if self._active_owner == owner:
            self._active_owner = None

    def _release_on_error_locked(self, owner: str, buttons: set[str] | None = None,
                                 keys: set[str] | None = None) -> None:
        state = self._owners.get(owner)
        held = (set(state.held) if state is not None else set()) | (buttons or set())
        pending = (set(state.keys) if state is not None else set()) | (keys or set())
        if not held and not pending:
            return
        try:
            self._send(self._release_inputs(held) + self._release_keys(pending))
            if state is not None:
                state.held.difference_update(held)
                state.keys.difference_update(pending)
            if self._active_owner == owner and state is not None and not state.held:
                self._active_owner = None
        except Exception:
            # Keep failed releases for the watchdog; never claim cleanup succeeded.
            if state is not None:
                state.held.update(held)
                state.keys.update(pending)
                self._active_owner = owner

    def _build_inputs_locked(self, owner: str, events: list[tuple], release: bool,
                             state: _Owner) -> tuple[list[_Input], set[str]]:
        simulated = set(state.held)
        inputs: list[_Input] = []
        for event in events:
            kind = event[0]
            if kind == "move":
                inputs.append(self._mouse(event[1], event[2], flags=_MOUSE_MOVE))
            elif kind == "wheel":
                if event[1]:
                    inputs.append(self._mouse(data=event[1], flags=_MOUSE_HWHEEL))
                if event[2]:
                    inputs.append(self._mouse(data=event[2], flags=_MOUSE_WHEEL))
            elif kind == "button":
                button, down = event[1], event[2]
                if down:
                    if self._active_owner not in (None, owner) or button in simulated:
                        raise ValueError("Another touchpad client owns the active button drag.")
                    simulated.add(button)
                    inputs.append(self._mouse(flags=_BUTTON_FLAGS[button][0]))
                else:
                    if button not in simulated or self._active_owner not in (None, owner):
                        raise ValueError("Touchpad button is not held by this client.")
                    simulated.remove(button)
                    inputs.append(self._mouse(flags=_BUTTON_FLAGS[button][1]))
            elif kind == "click":
                button, clicks = event[1], event[2]
                if self._active_owner not in (None, owner) or button in simulated:
                    raise ValueError("Another touchpad client owns the active button drag.")
                down, up = _BUTTON_FLAGS[button]
                for _ in range(clicks):
                    inputs.extend((self._mouse(flags=down), self._mouse(flags=up)))
            elif kind == "zoom":
                steps = event[1]
                if steps:
                    inputs.append(self._key(_VK["ctrl"]))
                    inputs.append(self._mouse(data=steps * 120, flags=_MOUSE_WHEEL))
                    inputs.append(self._key(_VK["ctrl"], True))
            else:
                inputs.extend(self._gesture_inputs(event[1]))
        if release:
            inputs.extend(self._release_inputs(simulated))
            simulated.clear()
        return inputs, simulated

    def apply(self, owner: str, seq: int, events: list[dict], release: bool = False) -> dict:
        if type(owner) is not str or not owner:
            raise ValueError("owner must be a non-empty string.")
        self._integer(seq, "seq", 0, 9007199254740991)
        with self._controller._input_lock:
            if self._closed:
                raise RuntimeError("Touchpad controller is closed.")
            try:
                checked, release = self._validate_batch(events, release)
                state = self._owners.get(owner)
                if state is None:
                    if len(self._owners) >= self._MAX_OWNERS:
                        raise RuntimeError("Too many active touchpad clients.")
                    state = _Owner()
                    self._owners[owner] = state
                if state.last_seq is not None and seq < state.last_seq:
                    raise ValueError("Touchpad sequence is older than the last accepted sequence.")
                if state.last_seq == seq:
                    state.last_activity = time.monotonic()
                    return {"ok": True, "seq": seq}
                inputs, held = self._build_inputs_locked(owner, checked, release, state)
                # Never replay a potentially partially inserted batch after an error.
                state.last_seq = seq
                state.last_activity = time.monotonic()
                try:
                    self._send(inputs)
                except Exception as error:
                    if isinstance(error, _InputDispatchError) and error.sent == 0:
                        self._release_on_error_locked(owner)
                    else:
                        # SendInput may have inserted a prefix, leaving a button or
                        # modifier physically down; release every control used by batch.
                        buttons = set(held)
                        keys = set()
                        for event in checked:
                            if event[0] in ("button", "click"):
                                buttons.add(event[1])
                            elif event[0] == "gesture":
                                keys.update(_GESTURES[event[1]])
                            elif event[0] == "zoom" and event[1]:
                                keys.add("ctrl")
                        self._release_on_error_locked(owner, buttons, keys)
                    raise
                state.held = held
                state.last_seq = seq
                state.last_activity = time.monotonic()
                if state.held:
                    self._active_owner = owner
                elif self._active_owner == owner:
                    self._active_owner = None
                return {"ok": True, "seq": seq}
            except Exception:
                self._release_on_error_locked(owner)
                raise

    def release_session(self, session: str) -> None:
        with self._controller._input_lock:
            error = None
            for owner, state in self._owners.items():
                if owner.startswith(session + ":"):
                    state.last_activity = 0
                    try:
                        self._release_owner_locked(owner, state)
                    except Exception as failure:
                        error = failure
            if error is not None:
                raise error

    def release_all(self) -> dict:
        with self._controller._input_lock:
            buttons = set().union(*(state.held for state in self._owners.values()))
            keys = set().union(*(state.keys for state in self._owners.values()))
            if buttons or keys:
                self._send(self._release_inputs(buttons) + self._release_keys(keys))
                for state in self._owners.values():
                    state.held.clear()
                    state.keys.clear()
            self._active_owner = None
            return {"ok": True}

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(0.25):
            with self._controller._input_lock:
                now = time.monotonic()
                for owner, state in list(self._owners.items()):
                    if now - state.last_activity >= self._LEASE_SECONDS:
                        if state.held or state.keys:
                            try:
                                self._release_owner_locked(owner, state)
                            except Exception:
                                continue
                        if not state.held and not state.keys and now - state.last_activity >= self._TOMBSTONE_SECONDS:
                            self._owners.pop(owner, None)
                            if self._active_owner == owner:
                                self._active_owner = None

    def close(self) -> None:
        with self._controller._input_lock:
            if self._closed:
                return
            try:
                self.release_all()
            finally:
                self._closed = True
                self._stop.set()
        self._watchdog.join(timeout=1.0)
