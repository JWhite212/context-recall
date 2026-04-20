"""Analytics computation engine."""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from src.action_items.repository import ActionItemRepository
from src.analytics.repository import AnalyticsRepository
from src.db.repository import MeetingRepository
from src.utils.config import AnalyticsConfig

logger = logging.getLogger("meetingmind.analytics")


class AnalyticsEngine:
    def __init__(
        self,
        config: AnalyticsConfig,
        meeting_repo: MeetingRepository,
        analytics_repo: AnalyticsRepository,
        action_item_repo: ActionItemRepository,
    ):
        self._config = config
        self._meeting_repo = meeting_repo
        self._analytics_repo = analytics_repo
        self._action_item_repo = action_item_repo

    async def refresh_period(self, period_type: str, period_start: str) -> None:
        """Recompute analytics for a specific period."""
        # Calculate start/end timestamps based on period_type
        start_dt = datetime.fromisoformat(period_start).replace(tzinfo=timezone.utc)
        if period_type == "daily":
            end_dt = start_dt + timedelta(days=1)
        elif period_type == "weekly":
            end_dt = start_dt + timedelta(days=7)
        else:  # monthly
            end_dt = start_dt + timedelta(days=30)

        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        # Query meetings in range
        cursor = await self._meeting_repo._db.conn.execute(
            "SELECT duration_seconds, word_count, attendees_json, series_id, started_at "
            "FROM meetings WHERE status = 'complete' AND started_at >= ? AND started_at < ?",
            (start_ts, end_ts),
        )
        meetings = [dict(r) for r in await cursor.fetchall()]

        total_meetings = len(meetings)
        total_duration = sum((m["duration_seconds"] or 0) for m in meetings) / 60
        total_words = sum((m["word_count"] or 0) for m in meetings)

        # Unique attendees
        all_attendees: set[str] = set()
        for m in meetings:
            try:
                for a in json.loads(m["attendees_json"] or "[]"):
                    if a.get("email"):
                        all_attendees.add(a["email"].lower())
            except (json.JSONDecodeError, TypeError):
                pass

        recurring_count = sum(1 for m in meetings if m.get("series_id"))
        recurring_ratio = recurring_count / total_meetings if total_meetings else 0.0

        hours = [datetime.fromtimestamp(m["started_at"], tz=timezone.utc).hour for m in meetings]
        busiest_hour = Counter(hours).most_common(1)[0][0] if hours else None

        await self._analytics_repo.upsert(
            period_type=period_type,
            period_start=period_start,
            total_meetings=total_meetings,
            total_duration_minutes=int(total_duration),
            total_words=total_words,
            unique_attendees=len(all_attendees),
            recurring_ratio=round(recurring_ratio, 3),
            action_items_created=0,  # Simplified for now
            action_items_completed=0,
            busiest_hour=busiest_hour,
        )

    async def get_period_summary(self, period_type: str, period_start: str):
        """Get analytics summary for a specific period."""
        return await self._analytics_repo.get_period(period_type, period_start)

    async def get_period_range(self, period_type: str, start: str, end: str):
        """Get analytics data for a range of periods."""
        return await self._analytics_repo.get_range(period_type, start, end)

    async def refresh_current_periods(self) -> None:
        """Refresh today, this week, this month."""
        now = datetime.now(timezone.utc)
        await self.refresh_period("daily", now.strftime("%Y-%m-%d"))
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        await self.refresh_period("weekly", week_start)
        await self.refresh_period("monthly", now.strftime("%Y-%m-01"))

    async def compute_load_score(self) -> dict:
        """Meeting load: current week vs rolling average."""
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        current = await self._analytics_repo.get_period("weekly", week_start)
        current_duration = current["total_duration_minutes"] if current else 0

        past_durations = []
        for i in range(1, self._config.rolling_window_weeks + 1):
            past_start = (now - timedelta(weeks=i, days=now.weekday())).strftime("%Y-%m-%d")
            row = await self._analytics_repo.get_period("weekly", past_start)
            if row:
                past_durations.append(row["total_duration_minutes"])

        if not past_durations:
            return {
                "ratio": 0.0,
                "label": "No data",
                "current_minutes": current_duration,
                "average_minutes": 0,
            }

        avg = sum(past_durations) / len(past_durations)
        ratio = current_duration / avg if avg > 0 else 0.0
        if ratio < 0.8:
            label = "Light week"
        elif ratio <= 1.2:
            label = "Normal"
        elif ratio <= 1.5:
            label = "Heavy"
        else:
            label = "Overloaded"
        return {
            "ratio": round(ratio, 2),
            "label": label,
            "current_minutes": current_duration,
            "average_minutes": round(avg, 1),
        }

    async def get_health_indicators(self) -> list[str]:
        indicators = []
        load = await self.compute_load_score()
        if load["ratio"] > 1.0 and load["label"] != "No data":
            pct = int((load["ratio"] - 1.0) * 100)
            indicators.append(
                f"You had {pct}% more meeting time than your "
                f"{self._config.rolling_window_weeks}-week average"
            )
        overdue = await self._action_item_repo.list_overdue()
        if overdue:
            indicators.append(f"You have {len(overdue)} overdue action items")
        return indicators

    async def get_most_met_people(self, limit: int = 10) -> list[dict]:
        cursor = await self._meeting_repo._db.conn.execute(
            "SELECT attendees_json FROM meetings "
            "WHERE status = 'complete' AND attendees_json != '[]' "
            "ORDER BY started_at DESC LIMIT 200"
        )
        rows = await cursor.fetchall()
        people_count: Counter = Counter()
        for row in rows:
            try:
                for a in json.loads(row["attendees_json"] or "[]"):
                    name = a.get("name", "").strip()
                    if name:
                        people_count[name] += 1
            except (json.JSONDecodeError, TypeError):
                pass
        return [
            {"name": name, "meeting_count": count}
            for name, count in people_count.most_common(limit)
        ]
