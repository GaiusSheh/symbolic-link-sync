"""Smoke test on this machine's real drive roots (same as the tray: C:\\ and G:\\).

Starts BackgroundWatcher drive threads, creates a dir under each drive's temp
location, checks the event arrives, samples private memory, then stops.

Run:  C:\\venvs\\sym-link-gui\\Scripts\\python.exe .dev/tests/watcher-leak/e2e_real_drives.py [seconds]
"""
import logging
import queue
import shutil
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

from core.watcher import BackgroundWatcher          # noqa: E402
from test_drive_root import private_mib, wait_until  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(name)s %(message)s")

DRIVES = [Path(p) for p in ("C:\\", "G:\\") if Path(p).exists()]
# G:\ is Google Drive: don't create probe dirs there (would sync to the cloud);
# for it we only check the thread holds a healthy pending read (failures == 0).
PROBE_PARENT = {"C:\\": Path.home() / "AppData/Local/Temp"}

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20
w = BackgroundWatcher(queue.Queue())
w.update_drive_roots(DRIVES)
time.sleep(2)

ok_all = True
for d in DRIVES:
    parent = PROBE_PARENT.get(str(d))
    if parent is None or not parent.is_dir():
        print(f"- {d}: no probe dir, event check skipped (health checked below)")
        continue
    probe = parent / f"symlisync-probe-{uuid.uuid4().hex[:8]}"
    probe.mkdir()
    got = wait_until(lambda: any(p.name == probe.name for p in w.get_recent_creates()), 10)
    shutil.rmtree(probe, ignore_errors=True)
    print(f"{'✓' if got else '✗'} {d}: create event for {probe.name}")
    ok_all &= got

samples = []
end = time.time() + duration
while time.time() < end:
    samples.append(private_mib())
    time.sleep(1)
print(f"private MiB over {duration:.0f}s: first={samples[0]:.1f} last={samples[-1]:.1f} "
      f"max={max(samples):.1f}")
alive = {str(d): t.is_alive() for d, t in w._drive_threads.items()}
print(f"threads alive: {alive}  failures: "
      f"{ {str(d): t._failures for d, t in w._drive_threads.items()} }")
stable = samples[-1] - samples[0] < 5
healthy = all(t._failures == 0 for t in w._drive_threads.values())
t0 = time.time()
w.stop()
print(f"stop() took {(time.time() - t0) * 1000:.0f} ms")
passed = ok_all and stable and healthy and all(alive.values())
print("PASS" if passed else "FAIL")
sys.exit(0 if passed else 1)
