"""Runtime hardening for the PyInstaller-frozen daemon."""

import os
import sys


def disable_bytecode_writing() -> None:
    """Stop the interpreter writing .pyc files into the signed bundle.

    Modules collected as source into the .app (torch, speechbrain via
    module_collection_mode='py', …) are compiled on first import, and
    CPython writes the __pycache__ next to them — INSIDE the codesigned
    bundle. Every added file breaks the codesign resource seal, and tccd
    silently refuses to raise TCC permission prompts for a process whose
    bundle fails validation (observed 2026-07-14: 781 'file added'
    violations, microphone dialog never appeared, permission pinned at
    not_determined).

    The env var carries the setting into multiprocessing helper children
    and any re-exec of the bundled interpreter, which would otherwise
    re-break the seal on their own imports.
    """
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
