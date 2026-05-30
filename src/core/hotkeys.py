"""Explorer-scoped global hotkeys via a low-level keyboard hook.

Windows has no API to scope a hotkey to another application, so we install a
WH_KEYBOARD_LL hook: when Ctrl+Q / Ctrl+J is pressed AND the foreground window
is a File Explorer window, we fire the callback and swallow the key; otherwise
the key passes through untouched (so Ctrl+Q still quits other apps, etc.).

The hook callback is kept minimal (modifier + foreground-class check, then hand
off to a queue) so it never slows down global typing. The real work (COM lookup
of the Explorer folder / selection) happens on the main thread.
"""

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN     = 0x0100
WM_SYSKEYDOWN  = 0x0104
WM_QUIT        = 0x0012
VK_CONTROL     = 0x11
VK_MENU        = 0x12   # ALT
_EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")

LRESULT   = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype  = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype     = LRESULT
user32.CallNextHookEx.argtypes    = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.restype   = ctypes.c_short
user32.GetAsyncKeyState.argtypes  = [ctypes.c_int]

# vkCode → action name
_KEYMAP = {ord("Q"): "paste", ord("J"): "inplace"}

# Module state (single hook per process)
_callback: Optional[Callable[[str, int], None]] = None
_hook = None
_proc_ref: Optional[HOOKPROC] = None    # must outlive the hook or it crashes
_thread: Optional[threading.Thread] = None
_thread_id: int = 0


def _foreground_is_explorer() -> int:
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return hwnd if buf.value in _EXPLORER_CLASSES else 0


def _hook_proc(nCode, wParam, lParam):
    if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        action = _KEYMAP.get(kb.vkCode)
        if action:
            ctrl = user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
            alt  = user32.GetAsyncKeyState(VK_MENU) & 0x8000
            if ctrl and not alt:
                hwnd = _foreground_is_explorer()
                if hwnd and _callback:
                    try:
                        _callback(action, int(hwnd))
                    except Exception:
                        logger.exception("hotkey callback failed")
                    return 1   # swallow: Explorer doesn't use Ctrl+Q / Ctrl+J
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def _run():
    global _hook, _proc_ref, _thread_id
    _thread_id = kernel32.GetCurrentThreadId()
    _proc_ref = HOOKPROC(_hook_proc)
    _hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _proc_ref, None, 0)
    if not _hook:
        logger.error("SetWindowsHookExW failed: %d", ctypes.get_last_error())
        return
    logger.info("keyboard hook installed (Ctrl+Q/Ctrl+J, Explorer-scoped)")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    if _hook:
        user32.UnhookWindowsHookEx(_hook)
        _hook = None
    logger.info("keyboard hook removed")


def start(callback: Callable[[str, int], None]) -> None:
    """Install the hook (idempotent). callback(action, hwnd) runs on the hook
    thread — it must only hand work to the main thread (e.g. a queue)."""
    global _callback, _thread
    _callback = callback
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, daemon=True, name="kbd-hook")
    _thread.start()


def stop() -> None:
    """Remove the hook and end its message loop."""
    global _callback
    _callback = None
    if _thread_id:
        user32.PostThreadMessageW(_thread_id, WM_QUIT, 0, 0)


def is_running() -> bool:
    return bool(_thread and _thread.is_alive() and _hook)
