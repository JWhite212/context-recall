"""Tests for the frozen-runtime hardening applied by the entrypoint."""

import os
import sys

from src.utils.frozen_runtime import disable_bytecode_writing


def test_disables_bytecode_writing(monkeypatch):
    """The frozen daemon must never write .pyc files: source modules
    collected into the signed .app (torch, speechbrain, …) get compiled on
    import, and the __pycache__ writes break the bundle's codesign resource
    seal — after which tccd silently refuses to show TCC permission prompts
    for the daemon (observed 2026-07-14: 781 'file added' seal violations,
    mic dialog never appeared)."""
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)

    disable_bytecode_writing()

    assert sys.dont_write_bytecode is True
    # The env var carries the setting into multiprocessing helper children
    # and any re-exec of the bundled interpreter.
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_entrypoint_hardens_before_heavy_imports():
    """__main__ must call disable_bytecode_writing() in its frozen block
    BEFORE importing src.main — heavy libraries compile their collected
    source on import, so ordering is load-bearing."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "src" / "__main__.py").read_text()
    call_pos = source.find("disable_bytecode_writing()")
    import_pos = source.find("from src.main import main")
    assert call_pos != -1, "__main__.py must call disable_bytecode_writing()"
    assert import_pos != -1
    assert call_pos < import_pos, (
        "bytecode writing must be disabled before src.main's heavy imports"
    )
