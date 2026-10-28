"""Live, destructive ClassIsland checks restricted to an explicitly marked disposable host."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
from contextlib import contextmanager
import time

import psutil

from classisland_bridge import ClassIslandBridge


@contextmanager
def ordinary_client():
    """Perform the actual native pipe access check under a restricted medium token."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.CreateRestrictedToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.HANDLE)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    advapi.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
    advapi.RevertToSelf.argtypes = []

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    token, restricted = wintypes.HANDLE(), wintypes.HANDLE()
    sid, admin_sid = ctypes.c_void_p(), ctypes.c_void_p()
    require = ClassIslandBridge._require
    try:
        require(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0xF01FF, ctypes.byref(token)))
        require(advapi.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(admin_sid)))
        disabled_admin = SidAndAttributes(admin_sid, 0)
        require(advapi.CreateRestrictedToken(token, 1, 1, ctypes.byref(disabled_admin), 0, None, 0, None, ctypes.byref(restricted)))
        require(advapi.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid)))
        label = SidAndAttributes(sid, 0x20)
        require(advapi.SetTokenInformation(restricted, 25, ctypes.byref(label), ctypes.sizeof(label) + advapi.GetLengthSid(sid)))
        require(advapi.ImpersonateLoggedOnUser(restricted))
        try:
            assert not ctypes.windll.shell32.IsUserAnAdmin()
            yield
        finally:
            require(advapi.RevertToSelf())
    finally:
        if sid: kernel.LocalFree(sid)
        if admin_sid: kernel.LocalFree(admin_sid)
        if restricted: kernel.CloseHandle(restricted)
        if token: kernel.CloseHandle(token)




def check(root):
    assert (root / ".remote-helper-check").is_file(), "Refusing to modify an unmarked ClassIsland data root"
    bridge = ClassIslandBridge()
    try:
        state = bridge.request("state")
        assert Path(psutil.Process(state["host_pid"]).exe()).is_relative_to(root), "This is not the disposable host"
        assert [r["start"] for r in state["lessons"]] == ["08:00", "08:55"]
        for action, values in [
            ("set_time_offset", {"seconds": True}),
            ("set_time_offset", {"seconds": 86401}),
            ("set_visible", {"visible": "true"}),
            ("notify", {"title": "invalid", "message": "invalid", "duration_seconds": True, "sound": False}),
        ]:
            try:
                bridge.request(action, **values)
                raise AssertionError(f"Malformed {action} command was accepted")
            except ValueError:
                pass
        original_offset, original_visible = state["time_offset_seconds"], state["visible"]
        original_subject = state["lessons"][0]["subject_id"]
        try:
            assert bridge.request("set_time_offset", seconds=3.5)["time_offset_seconds"] == 3.5
            assert bridge.request("set_time_offset", seconds=-2)["time_offset_seconds"] == -2
            assert json.loads((root / "data/Settings.json").read_text(encoding="utf-8-sig"))["TimeOffsetSeconds"] == -2
            assert bridge.request("set_visible", visible=False)["visible"] is False
            assert bridge.request("set_visible", visible=True)["visible"] is True
            stale = state["plan_id"]
            target_subject = next(subject["id"] for subject in state["subjects"] if subject["id"] != original_subject)
            changed = bridge.request("change_lesson", plan_id=stale, index=0, subject_id=target_subject)
            assert changed["lessons"][0]["subject_id"] == target_subject and changed["plan_id"] != stale
            stored = json.loads((root / "data/Profiles/Default.json").read_text(encoding="utf-8-sig"))
            assert stored["ClassPlans"]["44444444-4444-4444-4444-444444444444"]["Classes"][0]["SubjectId"] == "11111111-1111-1111-1111-111111111111"
            try:
                bridge.request("change_lesson", plan_id=stale, index=1, subject_id="11111111-1111-1111-1111-111111111111")
                raise AssertionError("Stale timetable selection was accepted")
            except RuntimeError as error:
                assert "课表已切换" in str(error)
            sent = bridge.request("notify", title="IPC 验收", message="ClassIsland 2.1 专属提醒渠道验收", duration_seconds=30, sound=False)
            assert sent["pending_remote_reminders"] == 1
            cleared = bridge.request("clear_notifications")
            assert not cleared["reminders_playing"] and cleared["pending_remote_reminders"] == 0
            print("PASS: actual host time offset persistence, UI visibility, temporary course replacement preserving source, stale selection rejection, dedicated-channel notification and clear-all.")
        finally:
            bridge.request("set_time_offset", seconds=original_offset)
            bridge.request("set_visible", visible=original_visible)
            restored = bridge.request("state")
            bridge.request("change_lesson", plan_id=restored["plan_id"], index=0, subject_id=original_subject)
        # A client which keeps an incomplete request open must not lock the sole pipe forever.
        require = bridge._require
        require(bridge._kernel.WaitNamedPipeW(bridge._pipe, 1000))
        blocker = bridge._kernel.CreateFileW(bridge._pipe, 0xC0000000, 0, None, 3, 0x40000000 | 0x00100000 | 0x00010000, None)
        assert blocker != ctypes.c_void_p(-1).value
        try:
            time.sleep(4.5)
            assert bridge.request("state")["connected"]
        finally:
            bridge._kernel.CloseHandle(blocker)
        print("PASS: incomplete client is disconnected at its deadline while its handle remains open; subsequent real pipe requests succeed.")
        with ordinary_client():
            result = bridge.request("state")
            assert result["connected"]
        print(f"PASS: actual medium-integrity, non-administrator effective token connects to {'elevated' if result['host_elevated'] else 'ordinary'} ClassIsland.")
    finally:
        bridge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    check(args.data_root.resolve())
