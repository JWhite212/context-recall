#!/usr/bin/env python3
"""
Generate MeetingMind app icons in all required sizes.

Design: indigo-to-violet gradient rounded square with a clean white
microphone and two concentric sound-wave arcs. Simple, recognisable
at small sizes, and consistent with macOS design conventions.

    python scripts/generate_icons.py
"""

import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ICON_DIR = Path(__file__).resolve().parent.parent / "ui" / "src-tauri" / "icons"

# Colour palette.
BG_TOP = (79, 70, 229)       # indigo-600
BG_BOTTOM = (124, 58, 237)   # violet-600
FG = (255, 255, 255)


def lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def draw_icon(size: int) -> Image.Image:
    """Draw the MeetingMind icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size  # shorthand

    # --- Rounded-rect background with gradient ---
    corner_radius = int(s * 0.22)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=corner_radius, fill=255)

    grad = Image.new("RGBA", (s, s))
    for y in range(s):
        t = y / max(s - 1, 1)
        c = lerp_color(BG_TOP, BG_BOTTOM, t)
        ImageDraw.Draw(grad).line([(0, y), (s - 1, y)], fill=(*c, 255))

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, mask=mask)
    draw = ImageDraw.Draw(img)

    # --- Microphone (centred, vertically offset upward) ---
    cx = s * 0.42          # slightly left of centre (waves go right)
    mic_top = s * 0.20
    mic_bot = s * 0.48
    mic_hw = s * 0.09      # half-width of capsule
    cap_r = mic_hw          # rounded cap radius

    # Capsule body.
    draw.rounded_rectangle(
        [cx - mic_hw, mic_top, cx + mic_hw, mic_bot],
        radius=int(cap_r),
        fill=(*FG, 255),
    )

    # Cradle (U-shaped arc under capsule).
    cradle_hw = mic_hw * 1.55
    cradle_top = mic_top + (mic_bot - mic_top) * 0.45
    cradle_bot = mic_bot + s * 0.10
    lw = max(2, int(s * 0.025))

    draw.arc(
        [cx - cradle_hw, cradle_top, cx + cradle_hw, cradle_bot],
        start=0, end=180,
        fill=(*FG, 255),
        width=lw,
    )

    # Stand (vertical line + base).
    stand_top = (cradle_top + cradle_bot) / 2 + s * 0.04
    stand_bot = s * 0.67
    draw.line([(cx, stand_top), (cx, stand_bot)], fill=(*FG, 255), width=lw)

    base_hw = mic_hw * 1.0
    draw.line([(cx - base_hw, stand_bot), (cx + base_hw, stand_bot)], fill=(*FG, 255), width=lw)

    # --- Sound wave arcs (emanating right from the mic) ---
    wave_cx = cx + s * 0.03
    wave_cy = (mic_top + mic_bot) / 2

    for i, (r_mult, alpha) in enumerate([(0.20, 220), (0.31, 160), (0.42, 100)]):
        r = s * r_mult
        draw.arc(
            [wave_cx - r, wave_cy - r, wave_cx + r, wave_cy + r],
            start=-45, end=45,
            fill=(*FG, alpha),
            width=max(2, int(s * 0.022)),
        )

    return img


def generate_all():
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    master = draw_icon(1024)
    master.save(ICON_DIR / "icon.png")
    print("  icon.png (1024x1024 master)")

    png_sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "Square30x30Logo.png": 30,
        "Square44x44Logo.png": 44,
        "Square71x71Logo.png": 71,
        "Square89x89Logo.png": 89,
        "Square107x107Logo.png": 107,
        "Square142x142Logo.png": 142,
        "Square150x150Logo.png": 150,
        "Square284x284Logo.png": 284,
        "Square310x310Logo.png": 310,
        "StoreLogo.png": 50,
    }

    for name, px in png_sizes.items():
        master.resize((px, px), Image.LANCZOS).save(ICON_DIR / name)
        print(f"  {name} ({px}x{px})")

    # macOS .icns via iconutil.
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            iconset = Path(tmpdir) / "icon.iconset"
            iconset.mkdir()
            for px in [16, 32, 64, 128, 256, 512, 1024]:
                resized = master.resize((px, px), Image.LANCZOS)
                if px <= 512:
                    resized.save(iconset / f"icon_{px}x{px}.png")
                if px >= 32:
                    half = px // 2
                    resized.save(iconset / f"icon_{half}x{half}@2x.png")
            subprocess.run(
                ["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_DIR / "icon.icns")],
                check=True,
            )
            print("  icon.icns (macOS)")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  Warning: .icns generation failed: {e}")

    # Windows .ico (multi-size).
    ico_imgs = [master.resize((px, px), Image.LANCZOS) for px in [16, 24, 32, 48, 64, 128, 256]]
    ico_imgs[0].save(
        ICON_DIR / "icon.ico",
        format="ICO",
        sizes=[(img.width, img.height) for img in ico_imgs],
        append_images=ico_imgs[1:],
    )
    print("  icon.ico (Windows)")
    print(f"\nDone — {ICON_DIR}")


if __name__ == "__main__":
    generate_all()
