"""Per-speaker talk-time statistics from a diarised transcript.

Pure computation over the stored ``transcript_json`` — no DB access,
no ML. Powers the talk-time bar in meeting detail and the balance
metric in analytics.
"""

from __future__ import annotations

import json


def compute_talk_stats(transcript_json: str | None) -> dict:
    """Return per-speaker speaking time, share, turns, longest monologue.

    A "turn" is a maximal run of consecutive segments by one speaker;
    the longest monologue is the longest such run in seconds. Segments
    without a speaker label are aggregated under "Unlabelled".
    """
    empty = {"speakers": [], "total_speaking_seconds": 0.0}
    if not transcript_json:
        return empty
    try:
        data = json.loads(transcript_json)
    except (ValueError, TypeError):
        return empty
    segments = data.get("segments", [])
    if not isinstance(segments, list) or not segments:
        return empty

    per_speaker: dict[str, dict] = {}
    prev_speaker: str | None = None
    run_seconds = 0.0

    def _close_run(speaker: str | None, seconds: float) -> None:
        if speaker is None:
            return
        entry = per_speaker[speaker]
        entry["turns"] += 1
        entry["longest_monologue_seconds"] = max(entry["longest_monologue_seconds"], seconds)

    for seg in segments:
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        duration = max(0.0, end - start)
        speaker = (seg.get("speaker") or "").strip() or "Unlabelled"
        entry = per_speaker.setdefault(
            speaker,
            {
                "speaker": speaker,
                "seconds": 0.0,
                "turns": 0,
                "longest_monologue_seconds": 0.0,
            },
        )
        entry["seconds"] += duration
        if speaker == prev_speaker:
            run_seconds += duration
        else:
            _close_run(prev_speaker, run_seconds)
            prev_speaker = speaker
            run_seconds = duration
    _close_run(prev_speaker, run_seconds)

    total = sum(e["seconds"] for e in per_speaker.values())
    speakers = []
    for entry in sorted(per_speaker.values(), key=lambda e: e["seconds"], reverse=True):
        speakers.append(
            {
                "speaker": entry["speaker"],
                "seconds": round(entry["seconds"], 2),
                "percent": round(100.0 * entry["seconds"] / total, 1) if total else 0.0,
                "turns": entry["turns"],
                "longest_monologue_seconds": round(entry["longest_monologue_seconds"], 2),
            }
        )
    return {"speakers": speakers, "total_speaking_seconds": round(total, 2)}
