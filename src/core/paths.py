"""Runtime path resolution for both source and PyInstaller frozen modes.

When frozen (--onefile exe), all runtime files live next to the exe.
When running from source, the layout mirrors the repo structure.
"""

import sys
from pathlib import Path


def _root() -> Path:
    """Directory that contains data/ and symlink-gui.log.

    Frozen: directory of the exe.
    Dev:    Sym-Link/ repo root (this file is gui/core/paths.py → 3 parents up).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent.parent


DATA_DIR      = _root() / "data"
SYMLINKS_JSON = DATA_DIR / "symlinks.json"
SETTINGS_JSON = DATA_DIR / "settings.json"
STATE_JSON    = DATA_DIR / "state.json"
LOG_PATH      = _root() / "symlink-gui.log"

# Ensure data/ exists at import time (safe to call repeatedly)
DATA_DIR.mkdir(exist_ok=True)
