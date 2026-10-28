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
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_SESSION_ID = 12
TOKEN_ELEVATION = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
LOGON_WITH_PROFILE = 1


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _Windows:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
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
        self.advapi32.DuplicateTokenEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, ctypes.c_int,
            ctypes.c_int, ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.DuplicateTokenEx.restype = wintypes.BOOL
        self.advapi32.CreateProcessWithTokenW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
        ]
        self.advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL
        self.user32.GetShellWindow.argtypes = []
        self.user32.GetShellWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.userenv.CreateEnvironmentBlock.argtypes = [
            ctypes.POINTER(wintypes.LPVOID), wintypes.HANDLE, wintypes.BOOL,
        ]
        self.userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
        self.userenv.DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
        self.userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

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
            shell_token = windows.token(process, TOKEN_QUERY | TOKEN_DUPLICATE, stack)
            if windows.token_dword(shell_token, TOKEN_SESSION_ID) != session:
                raise RuntimeError("Explorer belongs to a different Windows session.")
            if windows.token_dword(shell_token, TOKEN_ELEVATION):
                raise RuntimeError("Explorer is elevated; refusing to start an elevated helper.")
            primary = wintypes.HANDLE()
            windows.require(windows.advapi32.DuplicateTokenEx(
                shell_token, TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
                None, 2, 1, ctypes.byref(primary)), "DuplicateTokenEx")
            stack.callback(windows.kernel32.CloseHandle, primary)
            if (windows.token_dword(primary, TOKEN_ELEVATION)
                    or windows.token_dword(primary, TOKEN_SESSION_ID) != session):
                raise RuntimeError("The duplicated Explorer token is not a non-elevated session token.")
            environment = wintypes.LPVOID()
            windows.require(windows.userenv.CreateEnvironmentBlock(
                ctypes.byref(environment), primary, False), "CreateEnvironmentBlock")
            stack.callback(windows.userenv.DestroyEnvironmentBlock, environment)
            startup = STARTUPINFOW()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = "winsta0\\default"
            information = PROCESS_INFORMATION()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            windows.require(windows.advapi32.CreateProcessWithTokenW(
                primary, LOGON_WITH_PROFILE, executable, command_line,
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, environment,
                str(ROOT), ctypes.byref(startup), ctypes.byref(information)),
                "CreateProcessWithTokenW")
            stack.callback(windows.kernel32.CloseHandle, information.hProcess)
            stack.callback(windows.kernel32.CloseHandle, information.hThread)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"Cannot start the non-elevated PowerPoint helper: {error} "
            "Run Start-PPT.bat normally from the interactive Windows desktop, "
            "not with Run as administrator. No elevated fallback was started."
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
