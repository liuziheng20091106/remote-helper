"""Touchpad safety regression using a simulated Win32 input sink, no desktop clicks."""
from threading import RLock
from types import SimpleNamespace
import time

from touchpad_control import TouchpadController


class InputSink:
    def __init__(self):
        self.buttons = set()
        self.keys = set()
        self.moves = 0
        self.failures = 0

    def send(self, inputs):
        fail = self.failures > 0 and bool(inputs)
        if fail:
            self.failures -= 1
        for item in list(inputs)[:1] if fail else inputs:
            if item.type == 0:
                flags = item.mi.dwFlags
                for button, down, up in (("left", 2, 4), ("right", 8, 16), ("middle", 32, 64)):
                    if flags & down:
                        self.buttons.add(button)
                    if flags & up:
                        self.buttons.discard(button)
                if flags & 1:
                    self.moves += 1
            elif item.type == 1:
                if item.ki.dwFlags & 2:
                    self.keys.discard(item.ki.wVk)
                else:
                    self.keys.add(item.ki.wVk)
        if fail:
            raise RuntimeError("Simulated partial SendInput insertion")


def rejected(operation):
    try:
        operation()
    except (ValueError, RuntimeError):
        return
    raise AssertionError("Unsafe touchpad operation was accepted")


def check():
    pad = TouchpadController(SimpleNamespace(_input_lock=RLock()))
    sink = InputSink()
    pad._send = sink.send
    try:
        pad.apply("A", 1, [{"type": "button", "button": "left", "down": True}])
        assert sink.buttons == {"left"}
        rejected(lambda: pad.apply("B", 1, [{"type": "click", "button": "right", "clicks": 1}]))
        assert sink.buttons == {"left"}
        deadline = time.monotonic() + 4.5
        while sink.buttons:
            assert time.monotonic() < deadline, "Disconnected drag was not released"
            time.sleep(0.02)
        # Even after the lease expires, a delayed duplicate must not press again.
        pad.apply("A", 1, [{"type": "button", "button": "left", "down": True}])
        assert not sink.buttons
        before = sink.moves
        rejected(lambda: pad.apply("A", 2, [{"type": "move", "dx": 5, "dy": 0}, {"type": "move", "dx": True, "dy": 0}]))
        assert sink.moves == before, "A malformed batch must not partly move the pointer"
        sink.failures = 1
        rejected(lambda: pad.apply("A", 3, [{"type": "click", "button": "left", "clicks": 1}]))
        assert not sink.buttons, "Partly inserted click must receive button-up"
        pad.apply("A", 3, [{"type": "click", "button": "left", "clicks": 1}])
        assert not sink.buttons, "A failed sequence must never be replayed"
        # Ctrl-down succeeds, then immediate cleanup fails; watchdog must retry it.
        sink.failures = 3
        rejected(lambda: pad.apply("A", 4, [{"type": "zoom", "steps": 1}]))
        deadline = time.monotonic() + 4.5
        while sink.keys:
            assert time.monotonic() < deadline, "Modifier-up failure was not retried"
            time.sleep(0.02)
        assert not sink.keys
        pad.apply("A", 5, [{"type": "button", "button": "right", "down": True}])
        pad.release_all()
        assert not sink.buttons and not sink.keys
        pad.apply("session-A:pad", 1, [{"type": "button", "button": "left", "down": True}])
        pad.release_session("session-B")
        assert sink.buttons == {"left"}, "Another session's logout must not release this drag"
        pad.release_session("session-A")
        assert not sink.buttons, "Logout must immediately release its own drag"
    finally:
        pad.close()
    rejected(lambda: pad.apply("A", 6, []))
    print("PASS: drag isolation/expiry, delayed duplicate suppression, atomic validation, partial mouse/key recovery, pause and shutdown. Win32 sink simulated; no host click injected.")


if __name__ == "__main__":
    check()
