"""
Voice-embedding extraction via SpeechBrain's ECAPA-TDNN model.

Produces 192-dimensional speaker embeddings from audio windows, fully
locally (the model downloads once from HuggingFace without a token,
~80MB, cached under the app-support models dir). The heavy imports are
deferred and guarded so the daemon runs fine without speechbrain —
voice identification simply reports unavailable, the same graceful
degradation pattern as ``src/embeddings.py``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from src.utils.paths import app_support_dir

logger = logging.getLogger("contextrecall.voice")

EMBEDDING_DIM = 192


def is_voice_id_available() -> bool:
    """Check if speechbrain (and its torchaudio dependency) is installed."""
    try:
        import speechbrain  # noqa: F401
        import torchaudio  # noqa: F401

        return True
    except (ImportError, OSError):
        # OSError/FileNotFoundError, not just ImportError: a mis-bundled
        # speechbrain (source shipped but a submodule dir absent) raises
        # FileNotFoundError at import time from its lazy submodule loader.
        # Treat any import-time failure as "unavailable" so voice-ID degrades
        # gracefully instead of crashing the caller.
        return False


class VoiceEmbedder:
    """Embeds audio windows into speaker vectors using ECAPA-TDNN."""

    def __init__(self, model_source: str = "speechbrain/spkrec-ecapa-voxceleb") -> None:
        self._model_source = model_source
        self._model = None  # Lazy-loaded
        self._lock = threading.Lock()

    def _load_model(self) -> None:
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError:
            raise ImportError(
                "speechbrain is required for voice identification. "
                "Install it with: pip install speechbrain torchaudio"
            ) from None
        savedir = app_support_dir() / "models" / "speechbrain"
        savedir.mkdir(parents=True, exist_ok=True)
        self._model = EncoderClassifier.from_hparams(
            source=self._model_source,
            savedir=str(savedir),
            run_opts={"device": "cpu"},
        )
        logger.info("Loaded voice embedding model: %s", self._model_source)

    def _ensure_loaded(self) -> None:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load_model()

    def embed_windows(
        self, audio_path: Path, windows: list[tuple[float, float]]
    ) -> list[np.ndarray | None]:
        """Embed [(start_s, end_s), ...] windows of *audio_path*.

        Returns one L2-normalised vector per window, or ``None`` for
        windows that are out of range or effectively silent.
        """
        import soundfile as sf
        import torch

        self._ensure_loaded()

        results: list[np.ndarray | None] = []
        with sf.SoundFile(str(audio_path)) as f:
            sample_rate = f.samplerate
            for start_s, end_s in windows:
                start = max(0, int(start_s * sample_rate))
                end = min(f.frames, int(end_s * sample_rate))
                if end - start < int(0.25 * sample_rate):
                    results.append(None)
                    continue
                f.seek(start)
                audio = f.read(frames=end - start, dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
                if rms < 1e-5:
                    results.append(None)
                    continue
                wav = torch.from_numpy(audio).unsqueeze(0)
                with torch.no_grad():
                    emb = self._model.encode_batch(wav).squeeze().cpu().numpy()
                norm = np.linalg.norm(emb)
                results.append(emb / norm if norm > 1e-10 else None)
        return results
