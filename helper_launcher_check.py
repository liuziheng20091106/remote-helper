"""Check the real Explorer launch path and its privilege/session guards."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import Mock, patch

from helper_launcher import (ROOT, TOKEN_ELEVATION, TOKEN_SESSION_ID, _Windows,
                             _launch_from_desktop, start_helper)


def check_desktop_launch():
    windows = _Windows()
    hwnd = windows.user32.GetShellWindow()
    if not hwnd:
        raise RuntimeError("This check requires an interactive Explorer desktop")
    with tempfile.TemporaryDirectory(prefix="ppt-launch-中文 空格-") as directory:
        root = Path(directory)
        script, result = root / "launch check.py", root / "result.json"
        script.write_text("import ctypes,json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({'args':sys.argv[2:],'elevated':bool(ctypes.windll.shell32.IsUserAnAdmin())},ensure_ascii=False),encoding='utf-8')\n", encoding="utf-8")
        executable = ROOT / ".venv/Scripts/pythonw.exe"
        expected = ['页面 "三" & $() 中文', "trailing slash\\"]
        _launch_from_desktop([str(executable if executable.exists() else Path(sys.executable)), str(script), str(result), *expected], hwnd)
        deadline = time.monotonic() + 15
        while not result.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Explorer did not run the launch check")
            time.sleep(0.05)
        # The result file is written once; retry only incomplete JSON while it is being flushed.
        while True:
            try:
                payload = json.loads(result.read_text("utf-8"))
                break
            except ValueError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        assert payload["args"] == expected
        print({"PASS": "real Explorer desktop ShellExecute preserves paths and literal arguments", "child_elevated": payload["elevated"], "ordinary_helper_verified": not payload["elevated"]})


def check_launch_guards():
    windows = Mock()
    windows.token.side_effect = lambda process, access, stack: "current" if process == 1 else "shell"
    windows.token_dword.side_effect = lambda token, info: 2 if info == TOKEN_SESSION_ID else int(token == "current")
    windows.kernel32.GetCurrentProcess.return_value = 1
    windows.user32.GetShellWindow.return_value = 100
    windows.user32.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: setattr(pid._obj, "value", 88) or 1
    windows.kernel32.OpenProcess.return_value = 10
    windows.require.side_effect = lambda result, operation: result
    with tempfile.TemporaryDirectory() as directory, patch("helper_launcher._Windows", return_value=windows), patch("helper_launcher._launch_from_desktop") as launch:
        root = Path(directory)
        start_helper(root)
        command, hwnd = launch.call_args.args
        assert command[1:] == [str(ROOT / "ppt_helper.py"), "--root", str(root.resolve())] and hwnd == 100
        launch.reset_mock()
        windows.token_dword.side_effect = lambda token, info: 2 if info == TOKEN_SESSION_ID else 1
        try:
            start_helper(root)
            raise AssertionError("Elevated Explorer must be rejected")
        except RuntimeError as error:
            assert "Explorer is elevated" in str(error)
        launch.assert_not_called()
        windows.token_dword.side_effect = lambda token, info: (3 if token == "shell" else 2) if info == TOKEN_SESSION_ID else int(token == "current")
        try:
            start_helper(root)
            raise AssertionError("Different-session Explorer must be rejected")
        except RuntimeError as error:
            assert "different Windows session" in str(error)
        launch.assert_not_called()
    print("PASS: automatic launch dispatches to Explorer; elevated/different-session shell cannot launch the helper. Windows token values simulated for guard checks.")


if __name__ == "__main__":
    check_launch_guards()
    check_desktop_launch()
