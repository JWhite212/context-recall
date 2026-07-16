"""Availability guards must degrade on ANY import-time failure, not just ImportError.

Root cause (2026-07-16): a broken PyInstaller bundle ships speechbrain source
whose lazy submodule loader `os.listdir`s its own package dir. When that dir is
missing, `import speechbrain` raises **FileNotFoundError** (an OSError), not
ImportError — and `speechbrain/__init__.py` triggers this at import time. The
availability gates caught only ImportError, so instead of reporting
"unavailable" they raised uncaught, turning graceful degradation into a hard
failure ("voice-ID dead" rather than "voice-ID unavailable"). Same gap existed
in the mirrored `src.embeddings` gate.
"""

import builtins

from src.embeddings import is_embeddings_available
from src.voice.embedder import is_voice_id_available


def _raise_on_import(monkeypatch, target: str, exc: Exception) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == target or name.startswith(target + "."):
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_voice_id_unavailable_on_filenotfound(monkeypatch):
    # Exactly the deployed-bundle failure: speechbrain's __init__ listdir's a
    # missing package dir.
    _raise_on_import(
        monkeypatch,
        "speechbrain",
        FileNotFoundError(2, "No such file or directory", "/x/speechbrain/lobes"),
    )
    assert is_voice_id_available() is False


def test_voice_id_unavailable_on_oserror(monkeypatch):
    _raise_on_import(monkeypatch, "speechbrain", OSError("dylib load failed"))
    assert is_voice_id_available() is False


def test_voice_id_still_unavailable_on_importerror(monkeypatch):
    # Regression guard: the original behaviour must still hold.
    _raise_on_import(monkeypatch, "speechbrain", ImportError("no module"))
    assert is_voice_id_available() is False


def test_embeddings_unavailable_on_filenotfound(monkeypatch):
    _raise_on_import(
        monkeypatch,
        "sentence_transformers",
        FileNotFoundError(2, "No such file or directory", "/x/tokenizers"),
    )
    assert is_embeddings_available() is False
