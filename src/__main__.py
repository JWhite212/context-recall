"""Entry point for both `python -m src` and PyInstaller frozen binary."""

import os
import sys

# When running as a PyInstaller bundle, the PATH is minimal and may
# not include Homebrew or MacPorts. MLX Whisper shells out to ffmpeg
# for audio decoding, so we need it on PATH.
if getattr(sys, "frozen", False):
    _extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    _current = os.environ.get("PATH", "")
    _missing = [p for p in _extra_paths if p not in _current]
    if _missing:
        os.environ["PATH"] = _current + ":" + ":".join(_missing)

from src.main import main

main()
