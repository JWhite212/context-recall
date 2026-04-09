#!/usr/bin/env python3
"""Generate the DMG installer background image.

Produces a 660x400 indigo gradient with a drag-to-Applications arrow.
Requires Pillow: pip install Pillow
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 660
HEIGHT = 400

# Indigo gradient colours (OKLCH-inspired, hand-picked sRGB).
TOP_COLOR = (55, 48, 107)       # Deep indigo
BOTTOM_COLOR = (99, 89, 168)    # Lighter indigo


def _gradient(draw: ImageDraw.ImageDraw) -> None:
    """Draw a vertical linear gradient."""
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        r = int(TOP_COLOR[0] + (BOTTOM_COLOR[0] - TOP_COLOR[0]) * t)
        g = int(TOP_COLOR[1] + (BOTTOM_COLOR[1] - TOP_COLOR[1]) * t)
        b = int(TOP_COLOR[2] + (BOTTOM_COLOR[2] - TOP_COLOR[2]) * t)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))


def _arrow(draw: ImageDraw.ImageDraw) -> None:
    """Draw a right-pointing arrow in the centre."""
    cx, cy = WIDTH // 2, HEIGHT // 2
    shaft_len = 80
    head_size = 20

    # Shaft.
    draw.line(
        [(cx - shaft_len // 2, cy), (cx + shaft_len // 2, cy)],
        fill=(255, 255, 255, 180),
        width=4,
    )
    # Arrowhead.
    tip_x = cx + shaft_len // 2
    draw.polygon(
        [
            (tip_x, cy),
            (tip_x - head_size, cy - head_size // 2),
            (tip_x - head_size, cy + head_size // 2),
        ],
        fill=(255, 255, 255, 180),
    )


def _labels(draw: ImageDraw.ImageDraw) -> None:
    """Draw app icon placeholder label and Applications label."""
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except OSError:
        font = ImageFont.load_default()

    # Left label (app icon area).
    draw.text(
        (WIDTH // 2 - 140, HEIGHT // 2 + 40),
        "MeetingMind",
        fill=(255, 255, 255, 220),
        font=font,
        anchor="mt",
    )
    # Right label (Applications folder area).
    draw.text(
        (WIDTH // 2 + 140, HEIGHT // 2 + 40),
        "Applications",
        fill=(255, 255, 255, 220),
        font=font,
        anchor="mt",
    )


def main() -> None:
    img = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    _gradient(draw)
    _arrow(draw)
    _labels(draw)

    out = Path(__file__).parent / "background.png"
    img.save(out)
    print(f"Background image saved to {out} ({WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
