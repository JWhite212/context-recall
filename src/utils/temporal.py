"""Parse natural-language temporal references into Unix timestamp ranges."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

# Month names for "in January" etc.
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

# Pre-compiled patterns, ordered most-specific first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:past|last)\s+(\d+)\s+days?\b", re.IGNORECASE), "past_n_days"),
    (re.compile(r"\blast\s+quarter\b", re.IGNORECASE), "last_quarter"),
    (re.compile(r"\bthis\s+quarter\b", re.IGNORECASE), "this_quarter"),
    (re.compile(r"\blast\s+week\b", re.IGNORECASE), "last_week"),
    (re.compile(r"\bthis\s+week\b", re.IGNORECASE), "this_week"),
    (re.compile(r"\blast\s+month\b", re.IGNORECASE), "last_month"),
    (re.compile(r"\bthis\s+month\b", re.IGNORECASE), "this_month"),
    (re.compile(r"\blast\s+year\b", re.IGNORECASE), "last_year"),
    (re.compile(r"\bthis\s+year\b", re.IGNORECASE), "this_year"),
    (re.compile(r"\byesterday\b", re.IGNORECASE), "yesterday"),
    (re.compile(r"\btoday\b", re.IGNORECASE), "today"),
    (
        re.compile(
            r"\bin\s+(" + "|".join(calendar.month_name[1:]) + r")\b",
            re.IGNORECASE,
        ),
        "in_month",
    ),
]


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_temporal(query: str) -> tuple[str, float | None, float | None]:
    """Parse temporal references from a query string.

    Returns (cleaned_query, date_from, date_to) where dates are Unix timestamps.
    If no temporal reference found, returns (original_query, None, None).
    """
    now = datetime.now(timezone.utc)

    for pattern, kind in _PATTERNS:
        match = pattern.search(query)
        if not match:
            continue

        # Strip the matched temporal phrase from the query.
        cleaned = (query[: match.start()] + query[match.end() :]).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

        if kind == "past_n_days":
            n = int(match.group(1))
            d_from = _start_of_day(now - timedelta(days=n))
            return cleaned, d_from.timestamp(), now.timestamp()

        if kind == "last_week":
            # Monday-to-Sunday of the previous week.
            start_of_this_week = _start_of_day(now - timedelta(days=now.weekday()))
            end_of_last_week = start_of_this_week
            start_of_last_week = end_of_last_week - timedelta(days=7)
            return cleaned, start_of_last_week.timestamp(), end_of_last_week.timestamp()

        if kind == "this_week":
            start_of_this_week = _start_of_day(now - timedelta(days=now.weekday()))
            return cleaned, start_of_this_week.timestamp(), now.timestamp()

        if kind == "last_month":
            first_of_this_month = _start_of_day(now.replace(day=1))
            if now.month == 1:
                first_of_last_month = first_of_this_month.replace(year=now.year - 1, month=12)
            else:
                first_of_last_month = first_of_this_month.replace(month=now.month - 1)
            return cleaned, first_of_last_month.timestamp(), first_of_this_month.timestamp()

        if kind == "this_month":
            first_of_this_month = _start_of_day(now.replace(day=1))
            return cleaned, first_of_this_month.timestamp(), now.timestamp()

        if kind == "last_quarter":
            # Quarters: Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec
            current_q = (now.month - 1) // 3  # 0-based quarter index
            if current_q == 0:
                q_start = _start_of_day(now.replace(year=now.year - 1, month=10, day=1))
                q_end = _start_of_day(now.replace(month=1, day=1))
            else:
                q_start_month = (current_q - 1) * 3 + 1
                q_end_month = current_q * 3 + 1
                q_start = _start_of_day(now.replace(month=q_start_month, day=1))
                q_end = _start_of_day(now.replace(month=q_end_month, day=1))
            return cleaned, q_start.timestamp(), q_end.timestamp()

        if kind == "this_quarter":
            current_q = (now.month - 1) // 3
            q_start_month = current_q * 3 + 1
            q_start = _start_of_day(now.replace(month=q_start_month, day=1))
            return cleaned, q_start.timestamp(), now.timestamp()

        if kind == "yesterday":
            yesterday_start = _start_of_day(now - timedelta(days=1))
            yesterday_end = _start_of_day(now)
            return cleaned, yesterday_start.timestamp(), yesterday_end.timestamp()

        if kind == "today":
            today_start = _start_of_day(now)
            return cleaned, today_start.timestamp(), now.timestamp()

        if kind == "last_year":
            start = _start_of_day(now.replace(year=now.year - 1, month=1, day=1))
            end = _start_of_day(now.replace(month=1, day=1))
            return cleaned, start.timestamp(), end.timestamp()

        if kind == "this_year":
            start = _start_of_day(now.replace(month=1, day=1))
            return cleaned, start.timestamp(), now.timestamp()

        if kind == "in_month":
            month_name = match.group(1).lower()
            month_num = _MONTHS[month_name]
            year = now.year
            if month_num > now.month:
                year -= 1  # "in December" when it's April → last December.
            start = _start_of_day(now.replace(year=year, month=month_num, day=1))
            last_day = calendar.monthrange(year, month_num)[1]
            end = _start_of_day(now.replace(year=year, month=month_num, day=last_day)) + timedelta(
                days=1
            )
            return cleaned, start.timestamp(), end.timestamp()

    return query, None, None
