"""Pre-generate prep briefings for upcoming context-rich calendar events."""

import hashlib
import json
import logging

logger = logging.getLogger("contextrecall.prep")


def event_signature(emails: list[str]) -> str:
    """Order-stable hash of the event's attendee emails (for change detection)."""
    normalized = ",".join(sorted(e.lower() for e in emails if e))
    return hashlib.sha1(normalized.encode()).hexdigest()


def attendee_history_match(event_emails: set[str], recent_meetings: list[dict]) -> bool:
    """True if any event attendee appears in a prior completed meeting."""
    if not event_emails:
        return False
    for m in recent_meetings:
        try:
            prior = {
                a.get("email", "").lower()
                for a in json.loads(m.get("attendees_json") or "[]")
                if a.get("email")
            }
        except (ValueError, TypeError):
            prior = set()
        if event_emails & prior:
            return True
    return False


def matched_series_id(event_title: str, series: list[dict]) -> str | None:
    """Return the id of a series whose title normalizes-equal to the event title."""
    t = (event_title or "").strip().casefold()
    if not t:
        return None
    for s in series:
        if (s.get("title") or "").strip().casefold() == t:
            return s.get("id")
    return None


class PrepSweep:
    """Generate briefings for upcoming context-rich events lacking a current one."""

    def __init__(self, generator, cal_event_repo, meeting_repo, series_repo, prep_repo, config):
        self._generator = generator
        self._cal_event_repo = cal_event_repo
        self._meeting_repo = meeting_repo
        self._series_repo = series_repo
        self._prep_repo = prep_repo
        self._config = config

    async def run(self, now: float) -> int:
        end = now + self._config.lookahead_hours * 3600
        events = await self._cal_event_repo.list_by_range(now, end)
        if not events:
            return 0
        recent = await self._meeting_repo.list_recent_complete_with_attendees(limit=200)
        series = await self._series_repo.list_all()
        generated = 0
        for event in events:
            if generated >= self._config.max_per_sweep:
                break
            attendees = event.get("attendees") or []
            emails = [a.get("email", "") for a in attendees if a.get("email")]
            email_set = {e.lower() for e in emails}
            sig = event_signature(emails)
            uid = event["event_uid"]
            if await self._prep_repo.has_current_for_event(uid, sig):
                continue
            sid = matched_series_id(event.get("title", ""), series)
            if not (attendee_history_match(email_set, recent) or sid is not None):
                continue
            names = [a.get("name", "") for a in attendees]
            try:
                await self._generator.generate(
                    title=event.get("title", ""),
                    attendees=emails,
                    attendee_names=names,
                    series_id=sid,
                    calendar_event_uid=uid,
                    event_signature=sig,
                    expires_at=event.get("end_ts"),
                )
                generated += 1
            except Exception:
                logger.exception("Prep generation failed for event %s", uid)
        return generated
