import pytest

from src.prep.sweep import (
    PrepSweep,
    attendee_history_match,
    event_signature,
    matched_series_id,
)


def test_event_signature_is_order_stable():
    assert event_signature(["B@x.com", "a@X.com"]) == event_signature(["a@x.com", "b@x.com"])
    assert event_signature(["a@x.com"]) != event_signature(["a@x.com", "b@x.com"])


def test_attendee_history_match():
    recent = [{"attendees_json": '[{"email": "a@x.com"}]'}]
    assert attendee_history_match({"a@x.com"}, recent) is True
    assert attendee_history_match({"z@x.com"}, recent) is False
    assert attendee_history_match(set(), recent) is False


def test_matched_series_id():
    series = [{"id": "s1", "title": "Weekly Sync"}]
    assert matched_series_id("weekly sync", series) == "s1"
    assert matched_series_id("Ad-hoc chat", series) is None


class _FakeGen:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return "bid"


class _FakeGenRaisingForUid:
    """Generator that raises for a specific calendar_event_uid, succeeds for others."""

    def __init__(self, raise_for_uid):
        self.calls = []
        self._raise_for_uid = raise_for_uid

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        uid = kwargs.get("calendar_event_uid")
        if uid == self._raise_for_uid:
            raise ValueError(f"Simulated failure for {uid}")
        return "bid"


class _FakeCalRepo:
    def __init__(self, events):
        self._events = events

    async def list_by_range(self, start, end):
        return [e for e in self._events if start <= e["start_ts"] < end]


class _FakeMeetingRepo:
    def __init__(self, recent):
        self._recent = recent

    async def list_recent_complete_with_attendees(self, limit=200):
        return self._recent


class _FakeSeriesRepo:
    def __init__(self, series):
        self._series = series

    async def list_all(self):
        return self._series


class _FakePrepRepo:
    def __init__(self):
        self.seen = set()

    async def has_current_for_event(self, uid, sig):
        return (uid, sig) in self.seen


class _Cfg:
    lookahead_hours = 24
    max_per_sweep = 5


def _event(uid, start, title="M", emails=("a@x.com", "b@x.com")):
    return {
        "event_uid": uid,
        "title": title,
        "start_ts": start,
        "end_ts": start + 1800.0,
        "attendees": [{"name": e.split("@")[0], "email": e} for e in emails],
    }


@pytest.mark.asyncio
async def test_sweep_generates_for_context_rich_and_skips_cold():
    now = 1000.0
    events = [
        _event("HIST:1100", 1100.0, emails=("a@x.com",)),  # attendee history -> qualifies
        _event(
            "SER:1200", 1200.0, title="Weekly Sync", emails=("new@x.com",)
        ),  # series -> qualifies
        _event("COLD:1300", 1300.0, emails=("nobody@x.com",)),  # cold -> skipped
    ]
    gen = _FakeGen()
    sweep = PrepSweep(
        generator=gen,
        cal_event_repo=_FakeCalRepo(events),
        meeting_repo=_FakeMeetingRepo([{"attendees_json": '[{"email": "a@x.com"}]'}]),
        series_repo=_FakeSeriesRepo([{"id": "s1", "title": "Weekly Sync"}]),
        prep_repo=_FakePrepRepo(),
        config=_Cfg(),
    )
    n = await sweep.run(now)
    assert n == 2
    uids = {c["calendar_event_uid"] for c in gen.calls}
    assert uids == {"HIST:1100", "SER:1200"}
    # series match threads the series_id through
    ser_call = next(c for c in gen.calls if c["calendar_event_uid"] == "SER:1200")
    assert ser_call["series_id"] == "s1"


@pytest.mark.asyncio
async def test_sweep_skips_already_briefed_and_caps_per_tick():
    now = 1000.0
    events = [_event(f"E:{1100 + i}", 1100.0 + i, emails=("a@x.com",)) for i in range(4)]
    gen = _FakeGen()
    prep = _FakePrepRepo()
    # Mark the first event as already briefed (matching signature).
    prep.seen.add(("E:1100", event_signature(["a@x.com"])))
    cfg = _Cfg()
    cfg.max_per_sweep = 2
    sweep = PrepSweep(
        generator=gen,
        cal_event_repo=_FakeCalRepo(events),
        meeting_repo=_FakeMeetingRepo([{"attendees_json": '[{"email": "a@x.com"}]'}]),
        series_repo=_FakeSeriesRepo([]),
        prep_repo=prep,
        config=cfg,
    )
    n = await sweep.run(now)
    assert n == 2  # capped; and E:1100 was skipped as already briefed
    assert "E:1100" not in {c["calendar_event_uid"] for c in gen.calls}


@pytest.mark.asyncio
async def test_sweep_per_event_exception_does_not_abort():
    """Verify that one event's generate() failure doesn't stop the sweep."""
    now = 1000.0
    events = [
        _event("GOOD:1100", 1100.0, emails=("a@x.com",)),  # will succeed
        _event("FAIL:1200", 1200.0, emails=("a@x.com",)),  # will raise
    ]
    gen = _FakeGenRaisingForUid(raise_for_uid="FAIL:1200")
    sweep = PrepSweep(
        generator=gen,
        cal_event_repo=_FakeCalRepo(events),
        meeting_repo=_FakeMeetingRepo([{"attendees_json": '[{"email": "a@x.com"}]'}]),
        series_repo=_FakeSeriesRepo([]),
        prep_repo=_FakePrepRepo(),
        config=_Cfg(),
    )
    n = await sweep.run(now)
    # Only the successful event is counted.
    assert n == 1
    # Both events were attempted (generator.calls records all calls).
    assert len(gen.calls) == 2
    # Verify the non-raising event was indeed processed (it's in the calls).
    uids = {c["calendar_event_uid"] for c in gen.calls}
    assert "GOOD:1100" in uids
    assert "FAIL:1200" in uids
