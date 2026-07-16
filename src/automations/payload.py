"""Pure builders for the Circleback-compatible webhook payload."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from src.transcriber import Transcript

_STATUS_MAP = {"open": "PENDING", "completed": "DONE"}


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _attendees(attendees_json: str) -> list[dict]:
    try:
        raw = json.loads(attendees_json or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict):
            out.append({"name": e.get("name"), "email": e.get("email")})
        elif isinstance(e, str):
            out.append({"name": e, "email": None})
    return out


def _action_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        status = _STATUS_MAP.get(it.get("status", "open"))
        if status is None:  # e.g. 'cancelled' — omit
            continue
        assignee = it.get("assignee")
        assignee_obj = None
        if assignee and assignee != "unassigned":
            assignee_obj = {"name": assignee, "email": None}
        out.append(
            {
                "id": it.get("id"),
                "title": it.get("title", ""),
                "description": it.get("description", ""),
                "assignee": assignee_obj,
                "status": status,
            }
        )
    return out


def _insights(results: list[dict]) -> dict:
    grouped: dict[str, list] = {}
    for r in results:
        name = r.get("definition_name", "")
        if r.get("fields") is not None:
            entry = {"insight": r["fields"], "speaker": None}
        else:
            entry = {"insight": r.get("content", ""), "speaker": r.get("speaker") or None}
        grouped.setdefault(name, []).append(entry)
    return grouped


def _transcript(transcript_json) -> list[dict]:
    t = Transcript.from_dict(json.loads(transcript_json or "{}"))
    return [{"speaker": s.speaker, "text": s.text, "timestamp": s.start} for s in t.segments]


def _duration(meeting) -> float:
    d = getattr(meeting, "duration_seconds", None)
    if d:
        return float(d)
    started = getattr(meeting, "started_at", None)
    ended = getattr(meeting, "ended_at", None)
    if started and ended:
        return float(ended) - float(started)
    return 0.0


def build_circleback_payload(meeting, action_items, insights, *, include_transcript=False) -> dict:
    started = getattr(meeting, "started_at", None) or 0.0
    payload = {
        "id": getattr(meeting, "id", None),
        "name": getattr(meeting, "title", "") or "",
        "createdAt": datetime.fromtimestamp(float(started), tz=timezone.utc).isoformat(),
        "duration": _duration(meeting),
        "url": None,
        "tags": list(getattr(meeting, "tags", None) or []),
        "attendees": _attendees(getattr(meeting, "attendees_json", "") or ""),
        "notes": getattr(meeting, "summary_markdown", "") or "",
        "actionItems": _action_items(action_items or []),
        "insights": _insights(insights or []),
    }
    if include_transcript:
        payload["transcript"] = _transcript(getattr(meeting, "transcript_json", None))
    return payload
