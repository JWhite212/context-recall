"""Entry point for both `python -m src` and PyInstaller frozen binary."""

import multiprocessing
import os
import sys

# In the frozen binary, multiprocessing helper children (e.g. the
# resource_tracker spawned when a library creates a semaphore) re-exec
# THIS executable with interpreter-style args; without freeze_support()
# those args hit our argparse ("unrecognized arguments: -B -S -I -c
# from multiprocessing.resource_tracker import main") and the helper
# respawns in a loop.
multiprocessing.freeze_support()

# When running as a PyInstaller bundle: never write .pyc files — bundled
# source modules (torch, speechbrain, …) compile on import and the
# __pycache__ writes break the bundle's codesign resource seal, after
# which tccd silently refuses to show TCC prompts (mic dialog never
# appears). Must run before src.main's heavy imports below.
# Also: the PATH is minimal and may not include Homebrew or MacPorts.
# MLX Whisper shells out to ffmpeg for audio decoding, so we need it
# on PATH.
if getattr(sys, "frozen", False):
    from src.utils.frozen_runtime import disable_bytecode_writing

    disable_bytecode_writing()

    _extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    _current = os.environ.get("PATH", "")
    _missing = [p for p in _extra_paths if p not in _current]
    if _missing:
        os.environ["PATH"] = _current + ":" + ":".join(_missing)

# freeze_support() and the PATH fix must run before src.main's heavy
# imports, so this import is deliberately last.
from src.main import main  # noqa: E402

main()
