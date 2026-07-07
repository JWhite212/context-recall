"""
Temp-audio debris removal.

Recordings are captured into ``audio.temp_audio_dir`` (a Caches
directory) and the merged result is hard-linked into the durable audio
directory by the pipeline. Nothing ever cleaned the temp side, so it
accumulated every recording since first install: silent WAVs, orphaned
``_system``/``_mic`` source files, and 44-byte header-only stubs from
failed stream starts.

The rules here are deliberately conservative:

- ``meeting_*.wav`` files at WAV-header size (44 bytes) or less are
  removed at any age — a header with zero frames is garbage.
- Other ``meeting_*.wav`` files are removed only past ``max_age_days``
  (default 14): recent source files still let a re-process re-diarise.
- Files named differently are never touched; no recursion.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

WAV_HEADER_BYTES = 44
DEFAULT_MAX_AGE_DAYS = 14.0


def cleanup_temp_audio(
    temp_dir: Path | str,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    active_paths: Iterable[Path] = (),
    now: float | None = None,
) -> list[Path]:
    """Remove recording debris from the temp audio directory.

    ``active_paths`` (the in-flight recording's files) are never touched.
    Returns the paths actually removed. Never raises: individual removal
    failures are logged and skipped.
    """
    root = Path(temp_dir).expanduser()
    if not root.is_dir():
        return []

    reference = time.time() if now is None else now
    protected = {Path(p) for p in active_paths}
    removed: list[Path] = []

    for path in root.glob("meeting_*.wav"):
        if path in protected:
            continue
        try:
            stat = path.stat()
            is_stub = stat.st_size <= WAV_HEADER_BYTES
            is_stale = (reference - stat.st_mtime) > max_age_days * 86400
            if not (is_stub or is_stale):
                continue
            path.unlink()
            removed.append(path)
        except OSError:
            logger.warning("Could not remove temp audio file %s", path, exc_info=True)

    if removed:
        logger.info("Removed %d stale/empty temp audio file(s) from %s", len(removed), root)
    return removed
