"""Hybrid recurring meeting detection."""

import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher

from src.db.repository import MeetingRepository
from src.series.repository import SeriesRepository
from src.utils.config import SeriesConfig

logger = logging.getLogger("meetingmind.series.detector")


class HeuristicSeriesDetector:
    """Detect recurring meetings by attendee overlap, day, time, and title similarity."""

    def __init__(
        self, config: SeriesConfig, meeting_repo: MeetingRepository, series_repo: SeriesRepository
    ):
        self._config = config
        self._meeting_repo = meeting_repo
        self._series_repo = series_repo

    async def detect(self) -> list[str]:
        """Run heuristic detection on unlinked meetings. Returns new series IDs."""
        # 1. Fetch all complete meetings WHERE series_id IS NULL
        meetings = await self._meeting_repo.list_unlinked_complete_meetings()

        if len(meetings) < self._config.min_meetings_for_series:
            return []

        # 2. Parse attendees and datetime for each meeting
        for m in meetings:
            try:
                m["_attendees"] = set(
                    a.get("email", "").lower()
                    for a in json.loads(m["attendees_json"] or "[]")
                    if a.get("email")
                )
            except (json.JSONDecodeError, TypeError):
                m["_attendees"] = set()
            m["_dt"] = datetime.fromtimestamp(m["started_at"], tz=timezone.utc)

        # 3. Cluster: for each unlinked meeting, find similar ones
        clusters = []
        used = set()
        for i, m1 in enumerate(meetings):
            if i in used or not m1["_attendees"]:
                continue
            cluster = [m1]
            used.add(i)
            for j, m2 in enumerate(meetings):
                if j in used or j <= i or not m2["_attendees"]:
                    continue
                if self._is_similar(m1, m2):
                    cluster.append(m2)
                    used.add(j)
            if len(cluster) >= self._config.min_meetings_for_series:
                clusters.append(cluster)

        # 4. Create series for each cluster
        new_series_ids = []
        for cluster in clusters:
            title = cluster[0].get("title") or "Recurring Meeting"
            days = [m["_dt"].weekday() for m in cluster]
            typical_day = max(set(days), key=days.count)
            times = [m["_dt"].strftime("%H:%M") for m in cluster]
            typical_time = max(set(times), key=times.count)
            durations = [m["duration_seconds"] or 0 for m in cluster]
            typical_duration = int(sum(durations) / len(durations) / 60) if durations else None
            all_attendees = set()
            for m in cluster:
                all_attendees.update(m["_attendees"])

            series_id = await self._series_repo.create(
                title=title,
                detection_method="heuristic",
                typical_day_of_week=typical_day,
                typical_time=typical_time,
                typical_duration_minutes=typical_duration,
                typical_attendees_json=json.dumps(sorted(all_attendees)),
            )
            for m in cluster:
                await self._series_repo.link_meeting(m["id"], series_id)
            new_series_ids.append(series_id)
            logger.info("Detected series '%s' with %d meetings", title, len(cluster))

        return new_series_ids

    def _is_similar(self, m1: dict, m2: dict) -> bool:
        """Check if two meetings are likely the same recurring meeting."""
        a1, a2 = m1["_attendees"], m2["_attendees"]
        if not a1 or not a2:
            return False
        overlap = len(a1 & a2) / max(len(a1 | a2), 1)
        if overlap < self._config.attendee_overlap_threshold:
            return False

        day_diff = abs(m1["_dt"].weekday() - m2["_dt"].weekday())
        if day_diff > self._config.day_tolerance and day_diff < (7 - self._config.day_tolerance):
            return False

        hour_diff = abs(m1["_dt"].hour - m2["_dt"].hour)
        if hour_diff > self._config.time_tolerance_hours:
            return False

        t1 = m1.get("title", "") or ""
        t2 = m2.get("title", "") or ""
        if t1 and t2:
            similarity = SequenceMatcher(None, t1.lower(), t2.lower()).ratio()
            if similarity < self._config.title_similarity_threshold:
                return False

        return True
