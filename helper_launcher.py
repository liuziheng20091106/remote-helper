"""Start the PowerPoint helper as the interactive, non-elevated user."""

import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
TOKEN_QUERY = 0x0008
TOKEN_SESSION_ID = 12
TOKEN_ELEVATION = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NO_WINDOW = 0x08000000


class _Windows:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32.GetCurrentProcess.argtypes = []
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.user32.GetShellWindow.argtypes = []
        self.user32.GetShellWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    @staticmethod
    def require(result, operation):
        if not result:
            error = ctypes.WinError(ctypes.get_last_error())
            raise OSError(error.winerror, f"{operation}: {error.strerror}")
        return result

    def token(self, process, access, stack):
        token = wintypes.HANDLE()
        self.require(self.advapi32.OpenProcessToken(process, access, ctypes.byref(token)),
                     "OpenProcessToken")
        stack.callback(self.kernel32.CloseHandle, token)
        return token

    def token_dword(self, token, information_class):
        value = wintypes.DWORD()
        length = wintypes.DWORD()
        self.require(self.advapi32.GetTokenInformation(
            token, information_class, ctypes.byref(value), ctypes.sizeof(value),
            ctypes.byref(length)), "GetTokenInformation")
        if length.value != ctypes.sizeof(value):
            raise RuntimeError("Windows returned unexpected token information.")
        return value.value


def _launch_from_desktop(command, shell_hwnd):
    """Ask the existing Explorer process, rather than an elevated Shell.Application."""
    import comtypes
    from comtypes.client import CreateObject

    shell = windows = desktop = document = application = None
    comtypes.CoInitialize()
    try:
        shell = CreateObject("Shell.Application", dynamic=True)
        windows = shell.Windows()
        # SWC_DESKTOP=8, SWFO_NEEDDISPATCH=1; works without any folder window open.
        desktop = windows.FindWindowSW(0, 0, 8, 0, 1)
        if desktop is None or int(desktop.HWND) != shell_hwnd:
            raise RuntimeError("无法获取当前 Explorer 桌面启动接口。")
        document = desktop.Document
        application = document.Application
        application.ShellExecute(command[0], subprocess.list2cmdline(command[1:]), str(ROOT), "open", 0)
    except comtypes.COMError as error:
        raise RuntimeError("Explorer 桌面启动失败，请确认桌面可交互且模块目录可访问。") from error
    finally:
        application = document = desktop = windows = shell = None
        comtypes.CoUninitialize()


def start_helper(root: Path) -> None:
    """Launch only; the bridge waits for authenticated helper readiness."""
    if os.name != "nt":
        raise RuntimeError("The PowerPoint helper launcher requires Windows.")
    root = Path(root).resolve()
    script = ROOT / "ppt_helper.py"
    if not script.is_file():
        raise RuntimeError(f"PowerPoint helper is missing: {script}")
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    executable = str(pythonw if pythonw.is_file() else Path(sys.executable).resolve())
    command = [executable, str(script), "--root", str(root)]
    windows = _Windows()
    try:
        with ExitStack() as stack:
            current = windows.token(windows.kernel32.GetCurrentProcess(), TOKEN_QUERY, stack)
            if not windows.token_dword(current, TOKEN_ELEVATION):
                subprocess.Popen(command, cwd=str(ROOT), close_fds=True,
                                 creationflags=CREATE_NO_WINDOW)
                return
            session = windows.token_dword(current, TOKEN_SESSION_ID)
            shell = windows.user32.GetShellWindow()
            if not shell:
                raise RuntimeError("No interactive Explorer desktop is available.")
            shell_pid = wintypes.DWORD()
            windows.require(windows.user32.GetWindowThreadProcessId(shell, ctypes.byref(shell_pid)),
                            "GetWindowThreadProcessId")
            if not shell_pid.value:
                raise RuntimeError("The interactive Explorer process could not be identified.")
            process = windows.require(windows.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, shell_pid.value), "OpenProcess(Explorer)")
            stack.callback(windows.kernel32.CloseHandle, process)
            shell_token = windows.token(process, TOKEN_QUERY, stack)
            if windows.token_dword(shell_token, TOKEN_SESSION_ID) != session:
                raise RuntimeError("Explorer belongs to a different Windows session.")
            if windows.token_dword(shell_token, TOKEN_ELEVATION):
                raise RuntimeError("Explorer is elevated; refusing to start an elevated helper.")
            _launch_from_desktop(command, shell)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"无法自动启动普通权限 PPT 模块：{error} "
            "请在 PowerPoint 所在桌面正常运行 Start-PPT.bat。程序不会启动管理员权限模块。"
        ) from error


def _main():
    try:
        start_helper(ROOT)
    except Exception as error:
        message = str(error)
        log_path = ROOT / "logs" / "ppt-helper-startup.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                          backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger = logging.getLogger("ppt-helper-startup")
            logger.setLevel(logging.ERROR)
            logger.propagate = False
            logger.addHandler(handler)
            try:
                logger.exception("PowerPoint helper startup failed")
            finally:
                logger.removeHandler(handler)
                handler.close()
            message += f"\nSee {log_path}"
        except OSError as log_error:
            message += f"\nCould not write startup log: {log_error}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if "--message-box" in sys.argv[1:] and os.name == "nt":
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                          wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW(None, message, "PowerPoint helper startup failed", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
