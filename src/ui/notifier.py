"""Windows toast notifications with tkinter messagebox fallback."""

import logging
import tkinter as tk

logger = logging.getLogger(__name__)


def show_banner(root: tk.Tk, title: str, body: str, duration_ms: int = 4000) -> None:
    """Non-blocking in-app banner that auto-dismisses. Must be called from the main thread."""
    try:
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#323232")

        pad = 12
        tk.Label(win, text=title, fg="#FFFFFF", bg="#323232",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=pad, pady=(pad, 2))
        tk.Label(win, text=body, fg="#CCCCCC", bg="#323232",
                 font=("Segoe UI", 9), wraplength=300, justify="left").pack(
                     anchor="w", padx=pad, pady=(0, pad))

        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        w, h = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{sw - w - 20}+{sh - h - 60}")

        win.after(duration_ms, win.destroy)
    except Exception as e:
        logger.warning("show_banner failed: %s", e)


def send_toast(title: str, body: str) -> None:
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="SymLiSync",
            title=title,
            msg=body,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:
        logger.warning("winotify failed (%s), falling back to messagebox", e)
        try:
            import tkinter.messagebox as mb
            mb.showwarning(title, body)
        except Exception:
            pass
