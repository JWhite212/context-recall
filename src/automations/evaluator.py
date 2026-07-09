"""Pure matching of a meeting-context against automation rules.

No I/O, no DB, no LLM — every branch is unit-testable in isolation.
"""

import json


def domains_from_attendees(attendees_json: str) -> list[str]:
    """Extract lowercased email domains from a meeting's attendees JSON.

    Handles both the calendar shape (list of ``{"name","email"}`` dicts)
    and the plain-name/string shapes other paths store. Order-preserving,
    de-duplicated.
    """
    try:
        raw = json.loads(attendees_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    domains: list[str] = []
    for entry in raw:
        email = ""
        if isinstance(entry, dict):
            email = str(entry.get("email") or "")
        elif isinstance(entry, str):
            email = entry
        if "@" not in email:
            continue
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def build_meeting_context(meeting) -> dict:
    """Snapshot the fields automation conditions can match on."""
    return {
        "tags": list(getattr(meeting, "tags", None) or []),
        "client_id": getattr(meeting, "client_id", None),
        "project_id": getattr(meeting, "project_id", None),
        "title": getattr(meeting, "title", "") or "",
        "attendee_domains": domains_from_attendees(getattr(meeting, "attendees_json", "") or ""),
    }


def _condition_matches(context: dict, condition: dict) -> bool:
    field = condition.get("field")
    value = condition.get("value")
    if field == "tag":
        return value in context["tags"]
    if field == "client":
        return context["client_id"] == value
    if field == "project":
        return context["project_id"] == value
    if field == "title_contains":
        return bool(value) and str(value).lower() in context["title"].lower()
    if field == "attendee_domain":
        return str(value or "").strip().lower() in context["attendee_domains"]
    # Unknown field — never matches (forward-compatible / defensive).
    return False


def matches(context: dict, rule: dict) -> bool:
    conditions = rule.get("conditions") or []
    if not conditions:
        return False
    results = [_condition_matches(context, c) for c in conditions]
    if rule.get("match_mode") == "any":
        return any(results)
    return all(results)
