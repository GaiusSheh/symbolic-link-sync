"""Settings: load/save gui/settings.json + Windows auto-start via registry."""

import json
import sys
import winreg
from dataclasses import asdict, dataclass
from pathlib import Path

from core.paths import SETTINGS_JSON as _SETTINGS_PATH
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "SymLiSync"


@dataclass
class Settings:
    check_interval_minutes: int = 10
    autostart: bool = False
    close_to_tray: bool = True
    symlinks_path: str = ""   # empty = not yet configured; first-run wizard shown on startup


def load() -> Settings:
    if not _SETTINGS_PATH.exists():
        return Settings()
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return Settings(
            check_interval_minutes=int(data.get("check_interval_minutes", 10)),
            autostart=bool(data.get("autostart", False)),
            close_to_tray=bool(data.get("close_to_tray", True)),
            symlinks_path=str(data.get("symlinks_path", "")),
        )
    except Exception:
        return Settings()


def save(s: Settings) -> None:
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, indent=4)


def _exe_path() -> str:
    """Return the launch command to register for auto-start."""
    if getattr(sys, "frozen", False):
        return sys.executable
    # Running as script: use pythonw so no console window appears
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    main = Path(__file__).parent.parent / "main.py"
    return f'"{pythonw}" "{main}"'


def set_autostart(enabled: bool) -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                             access=winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, _exe_path())
        else:
            try:
                winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except OSError:
        pass


def get_autostart() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                             access=winreg.KEY_READ)
        winreg.QueryValueEx(key, _APP_NAME)
        winreg.CloseKey(key)
        return True
    except (OSError, FileNotFoundError):
        return False
