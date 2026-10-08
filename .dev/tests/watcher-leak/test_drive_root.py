"""Verification for issue #1: _DriveRootThread must not leak on read failures,
must back off, self-heal when its directory comes back, and stop promptly.

Run:  C:\\venvs\\sym-link-gui\\Scripts\\python.exe .dev/tests/watcher-leak/test_drive_root.py
"""
import ctypes
import logging
import os
import queue
import sys
import tempfile
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import win32con   # noqa: E402
import win32file  # noqa: E402

from core import watcher  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")


# ── memory probe ────────────────────────────────────────────────────────────
class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


_k32 = ctypes.windll.kernel32
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


def private_mib() -> float:
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise ctypes.WinError()
    return pmc.PrivateUsage / 2**20


def wait_until(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def set_backoff(lo: float, hi: float) -> None:
    watcher._BACKOFF_MIN_S = lo
    watcher._BACKOFF_MAX_S = hi


def new_thread(path: Path, cls=watcher._DriveRootThread):
    q: queue.Queue = queue.Queue()
    creates: deque = deque(maxlen=200)
    return cls(path, creates, q), creates, q


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}  {detail}")


# ── 1. leak regression ──────────────────────────────────────────────────────
def test_no_leak_on_read_failure(tmp: Path) -> None:
    """A regular FILE handle makes ReadDirectoryChangesW fail for real (error 87)."""
    fpath = tmp / "not_a_dir.bin"
    fpath.write_bytes(b"x")

    class FailingRead(watcher._DriveRootThread):
        opens = 0

        def _open(self):
            FailingRead.opens += 1
            return win32file.CreateFile(str(fpath), win32con.GENERIC_READ,
                                        win32con.FILE_SHARE_READ, None,
                                        win32con.OPEN_EXISTING,
                                        win32con.FILE_FLAG_OVERLAPPED, None)

    set_backoff(0.0, 0.0)
    logging.getLogger("core.watcher").setLevel(logging.ERROR)   # 300 failures → quiet
    t, _, _ = new_thread(tmp, FailingRead)
    before = private_mib()
    t.start()
    wait_until(lambda: FailingRead.opens >= 300, timeout=30)
    after = private_mib()
    t.stop()
    t.join(2)
    logging.getLogger("core.watcher").setLevel(logging.INFO)
    n = FailingRead.opens
    delta = after - before
    check("no leak on repeated read failure", n >= 300 and delta < 5.0,
          f"failures={n}  private Δ={delta:.2f} MiB (old code: ~1 MiB per failure)")


# ── 2. backoff ──────────────────────────────────────────────────────────────
def test_backoff() -> None:
    class FailingOpen(watcher._DriveRootThread):
        stamps: list[float] = []

        def _open(self):
            FailingOpen.stamps.append(time.time())
            raise OSError("simulated: drive not ready")

    set_backoff(0.1, 0.4)
    t, _, _ = new_thread(Path("Z:\\"), FailingOpen)
    t.start()
    time.sleep(1.55)          # attempts at ~0, 0.1, 0.3, 0.7, 1.1, 1.5
    t.stop()
    t.join(2)
    gaps = [round(b - a, 2) for a, b in zip(FailingOpen.stamps, FailingOpen.stamps[1:])]
    ok = 5 <= len(FailingOpen.stamps) <= 7 and gaps[:3] == sorted(gaps[:3]) and max(gaps) < 0.5
    check("exponential backoff, capped; open failure retried (late mount)", ok,
          f"attempts={len(FailingOpen.stamps)} gaps={gaps}")


# ── 3. functional + stop ────────────────────────────────────────────────────
def test_events_and_stop(tmp: Path) -> None:
    root = tmp / "func"
    root.mkdir()
    set_backoff(0.2, 0.5)
    t, creates, q = new_thread(root)
    t.start()
    time.sleep(watcher._HEALTHY_AFTER_S + 0.3)
    (root / "newdir").mkdir()
    (root / "newdir" / "nested").mkdir()
    got = wait_until(lambda: any(p.name == "nested" for _, p in creates), 3)
    refreshed = wait_until(lambda: not q.empty(), 3)
    check("dir create event recorded (recursive)", got,
          f"creates={[str(p.relative_to(root)) for _, p in creates]}")
    check("debounced ('refresh',) queued", refreshed and q.get_nowait() == ("refresh",))

    t0 = time.time()
    t.stop()
    t.join(2)
    dt = time.time() - t0
    check("stop() with pending read exits promptly", not t.is_alive() and dt < 1.0,
          f"{dt * 1000:.0f} ms")


# ── 4. self-heal ────────────────────────────────────────────────────────────
def test_self_heal(tmp: Path) -> None:
    root = tmp / "heal"
    root.mkdir()
    set_backoff(0.2, 0.5)
    t, creates, q = new_thread(root)
    t.start()
    time.sleep(watcher._HEALTHY_AFTER_S + 0.3)

    os.rmdir(root)                                   # watched dir disappears
    failed = wait_until(lambda: t._failures > 0, 3)
    check("read/open failure detected after dir removed", failed, f"failures={t._failures}")

    def recreate():
        try:
            root.mkdir()
            return True
        except OSError:                              # delete may still be pending
            return False
    wait_until(recreate, 5)
    healed = wait_until(lambda: t._failures == 0, 5)
    check("watch restored after dir comes back", healed)
    refreshed = wait_until(lambda: not q.empty(), 3)
    check("catch-up refresh queued on restore", refreshed)

    (root / "after_heal").mkdir()
    got = wait_until(lambda: any(p.name == "after_heal" for _, p in creates), 3)
    check("events flow again after restore", got)
    t.stop()
    t.join(2)
    check("thread exits after stop", not t.is_alive())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="symlisync-watch-") as d:
        tmp = Path(d)
        test_no_leak_on_read_failure(tmp)
        test_backoff()
        test_events_and_stop(tmp)
        test_self_heal(tmp)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
