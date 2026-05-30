"""Windows Explorer right-click integration via the classic (registry) menu.

Writes a cascading "SymLiSync" submenu under HKCU (no admin, no MSIX). On
Windows 11 these entries live under the "Show more options" classic menu.
Each item launches the exe with a --verb; the single-instance layer (ipc.py)
forwards the request to the running tray instance.
"""

import sys
import winreg
from pathlib import Path

from core import settings_manager as sm

_HKCU = winreg.HKEY_CURRENT_USER

# Cascading submenu key name placed under Directory\shell and Background\shell
_FOLDER_KEY = r"Software\Classes\Directory\shell\SymLiSync"
_BG_KEY     = r"Software\Classes\Directory\Background\shell\SymLiSync"

# Right-click ON a folder → clicked folder path is %1
_FOLDER_ITEMS = [
    ("01NewLinkTo",   "新建指向此文件夹的符号链接", "new-link-to",   "%1"),
    ("02ReplaceLink", "将此文件夹替换为符号链接",   "replace-link",  "%1"),
    ("03InplaceLink", "原地创建符号链接",           "inplace-link",  "%1"),
]
# Right-click on folder BACKGROUND (empty space) → current dir is %V
_BG_ITEMS = [
    ("01NewLinkHere", "在此处新建符号链接",   "new-link-here", "%V"),
    ("02PasteLink",   "粘贴为符号链接",       "paste-link",    "%V"),
]


def _icon_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable          # icon embedded in the exe
    return str(Path(__file__).parent.parent / "ui" / "assets" / "icon.ico")


def _write_cascade(menu_key: str, items: list) -> None:
    prefix = sm.launch_command_prefix()
    icon   = _icon_path()
    with winreg.CreateKey(_HKCU, menu_key) as k:
        winreg.SetValueEx(k, "MUIVerb",     0, winreg.REG_SZ, "SymLiSync")
        winreg.SetValueEx(k, "Icon",        0, winreg.REG_SZ, icon)
        winreg.SetValueEx(k, "subcommands", 0, winreg.REG_SZ, "")
    for name, label, verb, placeholder in items:
        item_key = f"{menu_key}\\shell\\{name}"
        with winreg.CreateKey(_HKCU, item_key) as ik:
            winreg.SetValueEx(ik, "MUIVerb", 0, winreg.REG_SZ, label)
            winreg.SetValueEx(ik, "Icon",    0, winreg.REG_SZ, icon)
        with winreg.CreateKey(_HKCU, f"{item_key}\\command") as ck:
            cmd = f'{prefix} --{verb} "{placeholder}"'
            winreg.SetValueEx(ck, "", 0, winreg.REG_SZ, cmd)


def _delete_tree(subkey: str) -> None:
    """Recursively delete an HKCU subkey (winreg has no recursive delete)."""
    try:
        with winreg.OpenKey(_HKCU, subkey, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(f"{subkey}\\{child}")
        winreg.DeleteKey(_HKCU, subkey)
    except FileNotFoundError:
        pass


def register() -> None:
    _write_cascade(_FOLDER_KEY, _FOLDER_ITEMS)
    _write_cascade(_BG_KEY, _BG_ITEMS)


def unregister() -> None:
    _delete_tree(_FOLDER_KEY)
    _delete_tree(_BG_KEY)


def is_registered() -> bool:
    try:
        winreg.OpenKey(_HKCU, _FOLDER_KEY).Close()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
