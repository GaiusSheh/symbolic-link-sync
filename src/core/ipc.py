"""Single-instance control + named-pipe IPC.

A second launch (e.g. from the Explorer context menu) detects the running
tray instance via a named mutex and forwards its request through a named pipe
instead of starting a second tray/watcher. The running instance owns all data.
"""

import json
import logging
import threading
from typing import Callable, Optional

import win32api
import win32event
import win32file
import win32pipe
import winerror

logger = logging.getLogger(__name__)

_MUTEX_NAME = "SymLiSync_singleton"
_PIPE_NAME  = r"\\.\pipe\SymLiSync"
_BUF_SIZE   = 8192


def acquire_singleton():
    """Create the singleton mutex.

    Returns (handle, already_running). The handle must be kept alive for the
    whole process lifetime (store it on the App) so the mutex stays held.
    """
    handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    already = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    return handle, already


def serve(on_message: Callable[[dict], None]) -> threading.Thread:
    """Start a daemon thread serving the named pipe.

    Each connecting client writes one JSON object; on_message is called with
    the decoded dict. on_message runs on the IPC thread — it must only hand
    work to the main thread (e.g. via the App queue), never touch widgets.
    """
    def _loop():
        while True:
            try:
                pipe = win32pipe.CreateNamedPipe(
                    _PIPE_NAME,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _BUF_SIZE, _BUF_SIZE, 0, None,
                )
            except Exception:
                logger.exception("CreateNamedPipe failed; IPC server stopping")
                return
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
                data = _read_all(pipe)
                if data:
                    try:
                        payload = json.loads(data.decode("utf-8"))
                        logger.info("IPC received: %s", payload)
                        on_message(payload)
                    except Exception:
                        logger.exception("Bad IPC payload: %r", data)
            except Exception:
                logger.exception("IPC connection error")
            finally:
                try:
                    win32file.CloseHandle(pipe)
                except Exception:
                    pass

    t = threading.Thread(target=_loop, daemon=True, name="ipc-server")
    t.start()
    return t


def _read_all(pipe) -> bytes:
    chunks = []
    while True:
        try:
            hr, chunk = win32file.ReadFile(pipe, _BUF_SIZE)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if len(chunk) < _BUF_SIZE:
            break
    return b"".join(chunks)


def send(payload: dict, timeout_ms: int = 1500) -> bool:
    """Connect to the running instance's pipe and send one JSON message.

    Returns False if no server is reachable (e.g. instance is shutting down).
    """
    data = json.dumps(payload).encode("utf-8")
    try:
        win32pipe.WaitNamedPipe(_PIPE_NAME, timeout_ms)
    except Exception:
        return False
    try:
        handle = win32file.CreateFile(
            _PIPE_NAME,
            win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
    except Exception:
        return False
    try:
        win32file.WriteFile(handle, data)
        return True
    except Exception:
        return False
    finally:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass
