"""The PocketMic icon (a microphone on a coloured disc), drawn with PIL for the tray, window and .exe."""

from __future__ import annotations

from PIL import Image, ImageDraw

GREEN = "#2e9e4f"   # connected
GREY = "#7a7f87"    # searching
RED = "#d64545"     # muted
BLUE = "#2f6fd0"    # application icon


def make_icon(color: str, size: int = 64) -> Image.Image:
    # Draw at 4x and downsample for smooth edges at tray sizes.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=color)
    white = "#ffffff"
    u = s / 64
    d.rounded_rectangle((24 * u, 10 * u, 40 * u, 38 * u), radius=8 * u, fill=white)   # capsule
    d.arc((17 * u, 20 * u, 47 * u, 46 * u), start=0, end=180, fill=white, width=int(3.5 * u))
    d.rectangle((30.5 * u, 45 * u, 33.5 * u, 52 * u), fill=white)                      # stem
    d.rounded_rectangle((23 * u, 51 * u, 41 * u, 55 * u), radius=2 * u, fill=white)   # base
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path) -> None:
    make_icon(BLUE, 256).save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                                         (64, 64), (128, 128), (256, 256)])
