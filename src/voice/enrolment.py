"""Voice-profile enrolment from a labelled meeting speaker.

When the user assigns a person to a transcript speaker, the segments
that speaker produced become an enrolment sample: their windows are
embedded from the meeting audio and averaged into one profile vector.
Future meetings match new voices against these samples.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("contextrecall.voice")

# Cap how much audio one enrolment embeds — beyond this the average
# stops improving and the request just gets slower.
_MAX_WINDOWS = 40


def extract_speaker_windows(
    transcript_json: str | None, speaker_label: str, min_seconds: float
) -> list[tuple[float, float]]:
    """Time windows (start, end) spoken by *speaker_label*."""
    if not transcript_json:
        return []
    try:
        data = json.loads(transcript_json)
    except (ValueError, TypeError):
        return []
    windows: list[tuple[float, float]] = []
    for seg in data.get("segments", []):
        if seg.get("speaker") != speaker_label:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        if end - start >= min_seconds:
            windows.append((start, end))
    return windows[:_MAX_WINDOWS]


def build_enrolment_sample(
    embedder, audio_path: Path, windows: list[tuple[float, float]]
) -> dict | None:
    """Average the window embeddings into one profile sample.

    Returns {"embedding", "segment_count", "duration_seconds"} or None
    when nothing usable could be embedded.
    """
    if not windows or not audio_path.exists():
        return None
    embeddings = embedder.embed_windows(audio_path, windows)
    usable = [(w, e) for w, e in zip(windows, embeddings) if e is not None]
    if not usable:
        return None
    mean = np.mean([e for _, e in usable], axis=0)
    norm = np.linalg.norm(mean)
    if norm < 1e-10:
        return None
    mean = mean / norm
    return {
        "embedding": [float(x) for x in mean],
        "segment_count": len(usable),
        "duration_seconds": round(sum(w[1] - w[0] for w, _ in usable), 2),
    }
