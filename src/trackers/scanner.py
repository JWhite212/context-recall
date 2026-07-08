"""Pure keyword scanning of transcripts against trackers."""

from __future__ import annotations

import re


def scan_transcript(transcript, trackers: list[dict]) -> list[dict]:
    """Word-boundary, case-insensitive keyword matches per segment.

    Returns hit dicts ready for TrackerRepository.replace_hits_for_meeting:
    one hit per (tracker, keyword, segment) — a keyword repeated within a
    segment counts once.
    """
    compiled: list[tuple[str, str, re.Pattern]] = []
    for tracker in trackers:
        if not tracker.get("enabled", True):
            continue
        for keyword in tracker.get("keywords", []):
            keyword = (keyword or "").strip()
            if len(keyword) < 2:
                continue
            compiled.append(
                (
                    tracker["id"],
                    keyword,
                    re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE),
                )
            )
    if not compiled:
        return []

    hits: list[dict] = []
    for i, seg in enumerate(transcript.segments):
        text = seg.text or ""
        if not text.strip():
            continue
        for tracker_id, keyword, pattern in compiled:
            if pattern.search(text):
                hits.append(
                    {
                        "tracker_id": tracker_id,
                        "segment_index": i,
                        "matched_keyword": keyword,
                        "matched_text": text.strip()[:300],
                        "start_time": seg.start,
                    }
                )
    return hits
