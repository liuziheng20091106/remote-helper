r"""Regression for reopening a different deck at the same path; no Office required.

Only IUnknown identity is simulated. This does not verify PowerPoint export or UI.
Run: .venv\Scripts\python.exe ppt_identity_check.py
"""
from __future__ import annotations

import ctypes
from types import SimpleNamespace

from powerpoint_control import PowerPointController


class Identity:
    def __init__(self):
        self.storage = ctypes.c_byte()

    def QueryInterface(self, interface):
        return ctypes.pointer(self.storage)


def check():
    # Separate live COM objects may have identical paths, slide IDs and dimensions.
    def document():
        return SimpleNamespace(Path=r"C:\slides", FullName=r"C:\slides\lesson.pptx",
                               Slides=SimpleNamespace(Count=3), _comobj=Identity())

    first, replacement = document(), document()
    controller = PowerPointController.__new__(PowerPointController)
    controller._presentation_identity = None
    controller._live_token = ""
    active = first
    controller._context = lambda: (None, active, None)
    original_token = controller._deck_id(first)
    assert controller._target(2, original_token)[1] is first
    original_bookmark = controller._bookmark_id(first, None)
    active = replacement
    for operation in (lambda: controller._target(2, original_token),
                      lambda: controller._image(2, original_token, "thumb", ""),
                      lambda: controller._goto(2, original_token)):
        try:
            operation()
        except RuntimeError as error:
            assert "已切换" in str(error)
        else:
            raise AssertionError("Stale requests reached a different presentation at the same path")
    replacement_token = controller._deck_id(replacement)
    assert replacement_token != original_token
    assert controller._target(2, replacement_token)[1] is replacement
    assert controller._bookmark_id(replacement, None) == original_bookmark
    print("PASS: same-path replacement rejects stale page/image/jump requests while preserving bookmark identity. IUnknown identities simulated; no Office export claim.")


if __name__ == "__main__":
    check()
