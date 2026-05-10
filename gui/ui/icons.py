"""Icon loading for tray and window.

Tray icons are colour-coded PNGs (tray_green/yellow/red.png).
Window icon is the standard blue chain (chain_256.png).
Run assets/generate_icons.py to regenerate all PNGs.
"""

from pathlib import Path
from PIL import Image

_ASSETS = Path(__file__).parent / "assets"
_cache: dict[str, Image.Image] = {}


def _load(name: str) -> Image.Image:
    if name not in _cache:
        _cache[name] = Image.open(_ASSETS / name).convert("RGBA")
    return _cache[name]


def tray_icon(color_key: str) -> Image.Image:
    """64×64 tray icon ('green' | 'yellow' | 'red')."""
    return _load(f"tray_{color_key}.png")


def app_icon(size: int = 256) -> Image.Image:
    """Window / taskbar icon (always blue)."""
    src = "chain_256.png" if size <= 256 else "chain_512.png"
    img = _load(src)
    if img.size != (size, size):
        return img.resize((size, size), Image.LANCZOS)
    return img
