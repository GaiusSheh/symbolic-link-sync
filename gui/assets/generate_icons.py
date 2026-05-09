"""Run once to regenerate PNG assets.

Output: gui/assets/chain_512.png  (chain_64.png is downscaled from it)

Design (all coords in pre-rotation space, canvas centre = 256, 256):
  - Two chain links, each = one semicircle + two horizontal straight lines
  - Links face each other with a gap in the middle
  - 6 short break marks in the gap
  - Everything rotated 45° around canvas centre
"""

from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from matplotlib.transforms import Affine2D
from PIL import Image

OUT = Path(__file__).parent
DPI = 100
SZ  = 512       # render canvas size

# ── Tweak these ───────────────────────────────────────────────────────────────

COLOR    = "#43cae9"   # chain colour
LW_LINK  = 22          # stroke width of the chain links
LW_BREAK = 16          # stroke width of break marks

# Chain link geometry (horizontal, centred at canvas centre before rotation)
R    = 60     # semicircle radius
LINE = 60    # straight-line segment length (each side)
GAP  = 180     # gap between the two open ends

# Break mark geometry (in pre-rotation space, origin at canvas centre)
BM_LEN        = 40     # length of every break mark
# Middle 2 marks: vertical, start at (0, ±BM_MID_Y0), end (0, ±(BM_MID_Y0+BM_LEN))
BM_MID_Y0     = 100
# Edge 4 marks: start at (±BM_EDGE_X, ±BM_EDGE_Y), angle ±BM_EDGE_ANG from vertical
BM_EDGE_X     = 40
BM_EDGE_Y     = 90
BM_EDGE_ANG   = 30     # degrees

ROTATION = 45          # final rotation of the whole icon

# ─────────────────────────────────────────────────────────────────────────────

cx = cy = SZ / 2
rot = Affine2D().rotate_deg_around(cx, cy, ROTATION)


def add_arc(ax, centre_x, centre_y, radius, theta1, theta2):
    arc = mpatches.Arc(
        (centre_x, centre_y), 2*radius, 2*radius,
        angle=0, theta1=theta1, theta2=theta2,
        color=COLOR, linewidth=LW_LINK,
    )
    arc.set_transform(rot + ax.transData)
    ax.add_patch(arc)


def add_line(ax, x0, y0, x1, y1, lw=LW_LINK):
    line = Line2D([x0, x1], [y0, y1],
                  color=COLOR, linewidth=lw,
                  solid_capstyle="round",
                  transform=rot + ax.transData)
    ax.add_line(line)


def make_fig(break_marks=True):
    fig = plt.figure(figsize=(SZ/DPI, SZ/DPI), dpi=DPI, facecolor="none")
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, SZ);  ax.set_ylim(0, SZ)
    ax.set_aspect("equal");  ax.axis("off")

    sc1x = cx - GAP/2 - LINE
    add_arc(ax, sc1x, cy, R, 90, 270)
    add_line(ax, sc1x, cy + R, cx - GAP/2, cy + R)
    add_line(ax, sc1x, cy - R, cx - GAP/2, cy - R)

    sc2x = cx + GAP/2 + LINE
    add_arc(ax, sc2x, cy, R, -90, 90)
    add_line(ax, cx + GAP/2, cy + R, sc2x, cy + R)
    add_line(ax, cx + GAP/2, cy - R, sc2x, cy - R)

    if break_marks:
        for sy in (+1, -1):
            add_line(ax, cx, cy + sy*BM_MID_Y0, cx, cy + sy*(BM_MID_Y0+BM_LEN), lw=LW_BREAK)
        for sx in (+1, -1):
            for sy in (+1, -1):
                ang = np.radians(BM_EDGE_ANG * sx)
                x0, y0 = cx + sx*BM_EDGE_X, cy + sy*BM_EDGE_Y
                add_line(ax, x0, y0,
                         x0 + np.sin(ang)*BM_LEN,
                         y0 + sy*np.cos(ang)*BM_LEN, lw=LW_BREAK)
    return fig


TRAY_COLORS = {
    "green":  "#4CAF50",   # all OK  — green
    "yellow": "#E6A817",   # warning — amber
    "red":    "#D94040",   # broken  — red
}


def main():
    global COLOR

    # Window / taskbar icon (uses COLOR defined at top of file)
    for px, name in [(512, "chain_512.png"), (256, "chain_256.png"), (64, "chain_64.png")]:
        fig   = make_fig()
        path  = OUT / name
        fig.savefig(path, format="png", transparent=True,
                    bbox_inches=None, pad_inches=0)
        plt.close(fig)
        if px != 512:
            img = Image.open(OUT / "chain_512.png").convert("RGBA")
            img.resize((px, px), Image.LANCZOS).save(path)
        print(f"Saved {path}")

    # ICO: large size from full design, small sizes from bold simplified version
    # ICO: embed all common DPI sizes so Windows never upscales a tiny frame
    base     = Image.open(OUT / "chain_512.png").convert("RGBA")
    ico_path = OUT / "icon.ico"
    ico_sizes = [256, 128, 64, 48, 40, 32, 24, 16]
    frames = [base.resize((s, s), Image.LANCZOS) for s in ico_sizes]
    frames[0].save(ico_path, format="ICO",
                   append_images=frames[1:],
                   sizes=[(s, s) for s in ico_sizes])
    print(f"Saved {ico_path}  ({len(ico_sizes)} sizes)")

    # Tray icon variants (64 px each, colour-coded)
    for key, col in TRAY_COLORS.items():
        COLOR = col
        fig   = make_fig()
        path  = OUT / f"tray_{key}.png"
        fig.savefig(path, format="png", transparent=True,
                    bbox_inches=None, pad_inches=0)
        plt.close(fig)
        img = Image.open(path).convert("RGBA")
        img.resize((64, 64), Image.LANCZOS).save(path)
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
