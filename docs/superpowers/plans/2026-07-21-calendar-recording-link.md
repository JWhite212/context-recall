# Recording ↔ Calendar-Entry Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user link a manually-recorded meeting to a calendar entry (both manually via pickers and automatically at record time), adopting the entry's calendar-derived metadata and collapsing the duplicate card in the calendar view.

**Architecture:** A new forward-link column `meetings.calendar_event_uid` (migration v24) is the source of truth. A small `src/calendar_link.py` service performs link/unlink — writing the forward link + adopting calendar-derived fields on the meeting, wiring the existing (dead) reverse link `calendar_events.recorded_meeting_id`, with move + 409-conflict semantics. The live matcher gains an `event_uid` so new recordings auto-link. Two API endpoints (`PUT`/`DELETE /api/meetings/{id}/calendar-link`) expose manual link/unlink; the UI collapses linked calendar events client-side and offers link pickers from both the recorded card and the calendar-entry popover.

**Tech Stack:** Python 3.12 (FastAPI, aiosqlite, pydantic v2), pytest + pytest-asyncio; React 19 + TypeScript + Vite + TanStack Query v5 + Tailwind, Vitest 4 + React Testing Library.

## Global Constraints

- **Non-destructive adoption:** linking overwrites _calendar-derived_ meeting fields (`calendar_event_title`, `attendees_json`, `teams_join_url`, `teams_meeting_id`, `calendar_confidence`→`1.0`) and sets `calendar_event_uid`; it **never** changes the meeting's `title`, `tags`, `client_id`/`project_id`, speaker mappings, `summary_markdown`, or the exported note. **No reprocess.**
- **Invariants:** one meeting ↔ ≤1 event; one event ↔ ≤1 recording. Re-linking a meeting **moves** it (clears the old event's reverse link). Linking to an event already tied to a _different_ recording → HTTP **409** (never silently steal).
- **Reverse link is best-effort:** mirror upsert / `set_recorded_meeting` failures are logged and swallowed — the forward link on the meeting is authoritative. Auto-link persists the forward link **only** (no mirror write in the pipeline thread).
- **event_uid identity** is `f"{event_identifier}:{int(start_ts)}"` — the matcher and the reader MUST produce it identically (shared helper `_event_uid`).
- **DB migrations are numbered and idempotent**; `SCHEMA_VERSION` head becomes **24**. `meetings` is in `_ALLOWED_TABLES`; `calendar_events` is **not** and is not altered.
- **No Claude/AI attribution** in any commit message.
- **Run all commands from the worktree root:** `/Users/jamiewhite/Documents/Personal/Projects/context-recall/.claude/worktrees/calendar-recording-link`. Activate the venv first: `source .venv/bin/activate` (or use the repo's existing interpreter). UI commands run from `ui/`.

---

## File Structure

**Create:**

- `src/calendar_link.py` — link/unlink service (`link_meeting_to_event`, `unlink_meeting_from_event`, `CalendarLinkConflict`).
- `tests/test_db_migration_v24.py` — v24 migration test.
- `tests/test_calendar_link.py` — link-service tests.
- `ui/src/components/calendar/CalendarLinkPicker.tsx` — reusable time-anchored picker (event-mode / recording-mode).
- `ui/src/components/calendar/__tests__/CalendarLinkPicker.test.tsx`
- `ui/src/components/calendar/__tests__/CalendarCollapse.test.tsx` — dedup/annotation.

**Modify (backend):**

- `src/db/database.py` — `SCHEMA_VERSION` 23→24; add `calendar_event_uid` in fresh-path + new `if current_version < 24:` block.
- `src/db/repository.py` — `_MUTABLE_COLUMNS` += `calendar_event_uid`; `MeetingRecord` field + `to_dict` + `from_row`; new `meeting_id_for_calendar_event`.
- `src/calendar_events/repository.py` — `set_recorded_meeting(event_uid, meeting_id: str | None)` (allow clearing).
- `src/calendar_matcher.py` — `_event_uid` helper; `CalendarMatch.event_uid`; populate it in `_do_match`.
- `src/calendar_events/reader.py` — use `_event_uid` (DRY parity).
- `src/main.py` — add `calendar_event_uid` to `calendar_fields`.
- `src/api/routes/reprocess.py` — re-supply `calendar_event_uid`.
- `src/api/routes/meetings.py` — `init` gains `calendar_event_repo`; `PUT`/`DELETE /api/meetings/{id}/calendar-link`; `meeting.calendar_link` emit.
- `src/api/server.py` — pass a `CalendarEventRepository` into `meetings_routes.init`.
- `tests/test_db_migration_v23.py` — update two `== SCHEMA_VERSION == 23` asserts → `24`.

**Modify (frontend):**

- `ui/src/lib/types.ts` — `Meeting.calendar_event_uid?`; `WSEvent` variant `meeting.calendar_link`.
- `ui/src/lib/api.ts` — `linkMeetingToCalendarEvent`, `unlinkMeetingFromCalendarEvent`.
- `ui/src/components/calendar/CalendarView.tsx` — compute `visibleEvents` (collapse) and pass to all sub-views.
- `ui/src/components/calendar/EventCard.tsx` — "↳ linked" annotation + `⋯` link menu.
- `ui/src/components/calendar/UpcomingEventCard.tsx` — "Assign a recording" action.
- `ui/src/components/meetings/MeetingDetail.tsx` — link/unlink calendar card.
- `ui/src/App.tsx` — invalidate `['calendar-events']` on link/complete; handle `meeting.calendar_link`.

---

## Task 1: Data model — migration v24 + repository field

**Files:**

- Modify: `src/db/database.py`
- Modify: `src/db/repository.py`
- Modify: `tests/test_db_migration_v23.py`
- Test: `tests/test_db_migration_v24.py` (create), `tests/test_repository.py` (append)

**Interfaces:**

- Produces: `meetings.calendar_event_uid TEXT DEFAULT ''`; `MeetingRecord.calendar_event_uid: str`; `MeetingRecord.to_dict()["calendar_event_uid"]`; `MeetingRepository.meeting_id_for_calendar_event(event_uid: str) -> str | None`; `SCHEMA_VERSION == 24`.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_v24.py`:

```python
"""v24 migration: meetings.calendar_event_uid forward link to a calendar entry."""

from src.db.database import SCHEMA_VERSION, Database


async def test_v23_db_migrates_to_v24_with_calendar_event_uid(tmp_path):
    db_path = tmp_path / "v23.db"
    db = Database(db_path=db_path)
    await db.connect()
    # Seed a meeting, then rewind to 23 so the v24 block runs on reconnect.
    await db.conn.execute(
        "INSERT INTO meetings (id, title, started_at, status, created_at, updated_at) "
        "VALUES ('m1', 'Chat', 1000.0, 'complete', 1.0, 1.0)"
    )
    await db.conn.execute("PRAGMA user_version = 23")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 24
        # Column exists and defaults to ''.
        cur = await db2.conn.execute("SELECT calendar_event_uid FROM meetings WHERE id = 'm1'")
        assert (await cur.fetchone())["calendar_event_uid"] == ""
    finally:
        await db2.close()


async def test_v24_migration_survives_missing_meetings_table(tmp_path):
    """A partial/legacy DB rewound below 24 without a meetings table must not
    hard-fail the ALTER (mirrors v21/v22/v23 defensive guards)."""
    db_path = tmp_path / "legacy.db"
    db = Database(db_path=db_path)
    await db.connect()
    await db.conn.execute("DROP TABLE meetings")
    await db.conn.execute("PRAGMA user_version = 23")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()  # must not raise
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 24
    finally:
        await db2.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v24.py -v`
Expected: FAIL — `SCHEMA_VERSION` is 23 (assert `== 24` fails) and column missing.

- [ ] **Step 3: Bump SCHEMA_VERSION and add the fresh-path column**

In `src/db/database.py` line 26: `SCHEMA_VERSION = 23` → `SCHEMA_VERSION = 24`.

In the fresh-DB fast path, immediately after the v23 lines (after `await self.conn.executescript(APP_METADATA_SQL)` at line 687, before `await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")`), add:

```python
            # Recording↔calendar-event link (v24).
            await _safe_add_column(self.conn, "meetings", "calendar_event_uid", "TEXT", "''")
```

- [ ] **Step 4: Add the incremental v24 block**

In `src/db/database.py`, replace the tail of `_migrate` — the v23 block's end plus its `else` (currently lines 983-985):

```python
            current_version = 23
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

with:

```python
            current_version = 23

        if current_version < 24:
            # Recording↔calendar-event link: forward reference from a meeting to
            # its calendar entry. Guard on the meetings table existing so minimal
            # migration fixtures don't hard-fail (mirrors v21/v22/v23).
            cur = await self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meetings'"
            )
            if await cur.fetchone() is not None:
                await _safe_add_column(self.conn, "meetings", "calendar_event_uid", "TEXT", "''")
            await self.conn.execute("PRAGMA user_version = 24")
            await self.conn.commit()
            logger.info("Database migrated to version 24 (recording↔calendar link)")
            current_version = 24
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 5: Fix the v23 migration-test assertions**

In `tests/test_db_migration_v23.py`, both assertions read `== SCHEMA_VERSION == 23`. A DB rewound to 22 now migrates through 24. Change both (lines 23 and 61) `== SCHEMA_VERSION == 23` → `== SCHEMA_VERSION == 24`.

- [ ] **Step 6: Run migration tests**

Run: `python3 -m pytest tests/test_db_migration_v24.py tests/test_db_migration_v23.py -v`
Expected: PASS.

- [ ] **Step 7: Write the failing repository test**

Append to `tests/test_repository.py`:

```python
async def test_calendar_event_uid_roundtrips_and_lookup(tmp_path):
    from src.db.database import Database
    from src.db.repository import MeetingRepository

    db = Database(db_path=tmp_path / "link.db")
    await db.connect()
    try:
        repo = MeetingRepository(db)
        mid = await repo.create_meeting(started_at=1000.0, status="complete")
        # Default empty, absent from any lookup.
        m = await repo.get_meeting(mid)
        assert m.calendar_event_uid == ""
        assert m.to_dict()["calendar_event_uid"] == ""
        assert await repo.meeting_id_for_calendar_event("EK1:1000") is None
        assert await repo.meeting_id_for_calendar_event("") is None
        # Write + read back + reverse lookup.
        await repo.update_meeting(mid, calendar_event_uid="EK1:1000")
        assert (await repo.get_meeting(mid)).calendar_event_uid == "EK1:1000"
        assert await repo.meeting_id_for_calendar_event("EK1:1000") == mid
    finally:
        await db.close()
```

- [ ] **Step 8: Run it to verify it fails**

Run: `python3 -m pytest tests/test_repository.py::test_calendar_event_uid_roundtrips_and_lookup -v`
Expected: FAIL — `calendar_event_uid` not on `MeetingRecord`; `update_meeting` rejects the column; `meeting_id_for_calendar_event` missing.

- [ ] **Step 9: Add the column to the repository layer**

In `src/db/repository.py`:

(a) Add `"calendar_event_uid",` to the `_MUTABLE_COLUMNS` frozenset (next to `"markdown_path",`).

(b) Add a dataclass field after `markdown_path: str = ""` (line 89):

```python
    calendar_event_uid: str = ""
```

(c) In `to_dict` (after the `"markdown_path": self.markdown_path,` line), add:

```python
            "calendar_event_uid": self.calendar_event_uid,
```

(d) In `from_row`, after the `markdown_path` block (line 182), add a defensive read:

```python
        calendar_event_uid = ""
        if "calendar_event_uid" in row.keys():
            calendar_event_uid = row["calendar_event_uid"] or ""
```

and pass `calendar_event_uid=calendar_event_uid,` in the `return cls(...)` call (after `markdown_path=markdown_path,`).

(e) Add the lookup method to `MeetingRepository` (after `get_meetings_by_ids`, ~line 314):

```python
    async def meeting_id_for_calendar_event(self, event_uid: str) -> str | None:
        """Return the id of the meeting linked to ``event_uid``, or None.

        Backs the link-conflict check: an event may be linked to at most one
        recording. Uses the forward link on meetings, so it is correct even
        when the event is absent from the calendar_events mirror.
        """
        if not event_uid:
            return None
        cursor = await self._db.conn.execute(
            "SELECT id FROM meetings WHERE calendar_event_uid = ? LIMIT 1",
            (event_uid,),
        )
        row = await cursor.fetchone()
        return row["id"] if row else None
```

- [ ] **Step 10: Run the repository test**

Run: `python3 -m pytest tests/test_repository.py::test_calendar_event_uid_roundtrips_and_lookup -v`
Expected: PASS.

- [ ] **Step 11: Regression sweep of the DB/repo suite**

Run: `python3 -m pytest tests/test_repository.py tests/test_db_migration_v24.py tests/test_db_migration_v23.py -q`
Expected: PASS (no regressions from the added `to_dict` key).

- [ ] **Step 12: Commit**

```bash
git add src/db/database.py src/db/repository.py tests/test_db_migration_v24.py tests/test_db_migration_v23.py tests/test_repository.py
git commit -m "feat(db): v24 calendar_event_uid forward link + repo lookup"
```

---

## Task 2: Link service (`src/calendar_link.py`)

**Files:**

- Create: `src/calendar_link.py`
- Modify: `src/calendar_events/repository.py`
- Test: `tests/test_calendar_link.py` (create)

**Interfaces:**

- Consumes: `MeetingRepository.get_meeting`, `.update_meeting`, `.meeting_id_for_calendar_event` (Task 1); `CalendarEventRepository.upsert`, `.set_recorded_meeting`; `CalendarEvent` (reader), `MeetingRecord` (repository).
- Produces:
  - `CalendarLinkConflict(Exception)`
  - `async def link_meeting_to_event(meeting_repo, calendar_event_repo, meeting: MeetingRecord, event: CalendarEvent, *, source: str = "manual") -> None`
  - `async def unlink_meeting_from_event(meeting_repo, calendar_event_repo, meeting: MeetingRecord) -> None`
  - `CalendarEventRepository.set_recorded_meeting(event_uid, meeting_id: str | None)` now clears on `None`.

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_calendar_link.py`:

```python
"""Link/unlink service: forward + reverse link, adoption, move, conflict."""

import json

import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_link import (
    CalendarLinkConflict,
    link_meeting_to_event,
    unlink_meeting_from_event,
)
from src.db.database import Database
from src.db.repository import MeetingRepository


def _event(uid="EK1:1000"):
    return CalendarEvent(
        event_uid=uid,
        title="Quick Catch-Up",
        start_ts=1000.0,
        end_ts=2800.0,
        attendees=[{"name": "Jamie", "email": "j@x.com"}, {"name": "Amelia", "email": "a@x.com"}],
        organizer=None,
        join_url="https://teams.microsoft.com/l/meetup-join/x",
        meeting_id="19:mtg@thread.v2",
        calendar_name="Work",
    )


async def _fixture(tmp_path):
    db = Database(db_path=tmp_path / "cl.db")
    await db.connect()
    return db, MeetingRepository(db), CalendarEventRepository(db)


async def test_link_adopts_calendar_fields_and_sets_both_links(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        # A manual meeting title must be preserved.
        await mrepo.update_meeting(mid, title="Amelia Monthly Check-In", title_source="manual")
        meeting = await mrepo.get_meeting(mid)

        await link_meeting_to_event(mrepo, crepo, meeting, _event(), source="manual")

        m = await mrepo.get_meeting(mid)
        assert m.calendar_event_uid == "EK1:1000"
        assert m.calendar_event_title == "Quick Catch-Up"
        assert json.loads(m.attendees_json) == _event().attendees
        assert m.teams_join_url == _event().join_url
        assert m.teams_meeting_id == "19:mtg@thread.v2"
        assert m.calendar_confidence == 1.0
        assert m.title == "Amelia Monthly Check-In"  # preserved
        # Reverse link written + event mirrored.
        assert await mrepo.meeting_id_for_calendar_event("EK1:1000") == mid
        rows = await crepo.list_by_range(0.0, 5000.0)
        assert rows[0]["recorded_meeting_id"] == mid
    finally:
        await db.close()


async def test_relink_moves_and_clears_old_event(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        meeting = await mrepo.get_meeting(mid)
        await link_meeting_to_event(mrepo, crepo, meeting, _event("EK1:1000"))
        meeting = await mrepo.get_meeting(mid)
        await link_meeting_to_event(mrepo, crepo, meeting, _event("EK2:2000"))

        m = await mrepo.get_meeting(mid)
        assert m.calendar_event_uid == "EK2:2000"
        by_uid = {r["event_uid"]: r for r in await crepo.list_by_range(0.0, 5000.0)}
        assert by_uid["EK1:1000"]["recorded_meeting_id"] is None
        assert by_uid["EK2:2000"]["recorded_meeting_id"] == mid
    finally:
        await db.close()


async def test_link_conflict_when_event_linked_to_other_meeting(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        m1 = await mrepo.create_meeting(started_at=1005.0, status="complete")
        m2 = await mrepo.create_meeting(started_at=1010.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(m1), _event())
        with pytest.raises(CalendarLinkConflict):
            await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(m2), _event())
    finally:
        await db.close()


async def test_relink_same_event_is_idempotent(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        # Re-linking the SAME meeting to the SAME event must not 409.
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        assert (await mrepo.get_meeting(mid)).calendar_event_uid == "EK1:1000"
    finally:
        await db.close()


async def test_unlink_clears_both_sides(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        await unlink_meeting_from_event(mrepo, crepo, await mrepo.get_meeting(mid))
        assert (await mrepo.get_meeting(mid)).calendar_event_uid == ""
        rows = await crepo.list_by_range(0.0, 5000.0)
        assert rows[0]["recorded_meeting_id"] is None
    finally:
        await db.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_calendar_link.py -v`
Expected: FAIL — `src.calendar_link` module does not exist.

- [ ] **Step 3: Allow `set_recorded_meeting` to clear**

In `src/calendar_events/repository.py`, change the signature (line 91) to accept `None` (unchanged body — sqlite binds `None`→NULL):

```python
    async def set_recorded_meeting(self, event_uid: str, meeting_id: str | None) -> None:
```

- [ ] **Step 4: Create the link service**

Create `src/calendar_link.py`:

```python
"""Link/unlink a recorded meeting to a calendar entry.

Shared by the manual API endpoint (bidirectional) and — for the forward
link only — the auto-link path at record time. Kept pure over the two
repositories so it is unit-testable against a real SQLite DB.
"""

import json
import logging

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.db.repository import MeetingRecord, MeetingRepository

logger = logging.getLogger("contextrecall.calendar_link")


class CalendarLinkConflict(Exception):
    """The target calendar event is already linked to another recording."""


async def link_meeting_to_event(
    meeting_repo: MeetingRepository,
    calendar_event_repo: CalendarEventRepository,
    meeting: MeetingRecord,
    event: CalendarEvent,
    *,
    source: str = "manual",
) -> None:
    """Link ``meeting`` to ``event``: forward link + adopt calendar-derived
    fields + (best-effort) reverse link. Moves an existing link. Raises
    ``CalendarLinkConflict`` if the event is already tied to another meeting.
    """
    owner = await meeting_repo.meeting_id_for_calendar_event(event.event_uid)
    if owner and owner != meeting.id:
        raise CalendarLinkConflict(
            f"Calendar event {event.event_uid} is already linked to meeting {owner}"
        )

    old_uid = meeting.calendar_event_uid or ""

    # Forward link + adopt calendar-derived fields. User-authored fields
    # (title, tags, assignment, speakers) are deliberately untouched.
    await meeting_repo.update_meeting(
        meeting.id,
        calendar_event_uid=event.event_uid,
        calendar_event_title=event.title,
        attendees_json=json.dumps(event.attendees or []),
        teams_join_url=event.join_url,
        teams_meeting_id=event.meeting_id,
        calendar_confidence=1.0,
    )

    # Reverse link (best-effort): mirror the event, then mark it recorded.
    try:
        await calendar_event_repo.upsert(event)
        await calendar_event_repo.set_recorded_meeting(event.event_uid, meeting.id)
        if old_uid and old_uid != event.event_uid:
            await calendar_event_repo.set_recorded_meeting(old_uid, None)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Reverse calendar link failed for meeting %s → %s (%s): %s",
            meeting.id,
            event.event_uid,
            source,
            e,
        )


async def unlink_meeting_from_event(
    meeting_repo: MeetingRepository,
    calendar_event_repo: CalendarEventRepository,
    meeting: MeetingRecord,
) -> None:
    """Clear the meeting's forward link and the event's reverse link."""
    old_uid = meeting.calendar_event_uid or ""
    await meeting_repo.update_meeting(meeting.id, calendar_event_uid="")
    if old_uid:
        try:
            await calendar_event_repo.set_recorded_meeting(old_uid, None)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Reverse unlink failed for event %s: %s", old_uid, e)
```

- [ ] **Step 5: Run the service tests**

Run: `python3 -m pytest tests/test_calendar_link.py -v`
Expected: PASS (all 5).

- [ ] **Step 6: Commit**

```bash
git add src/calendar_link.py src/calendar_events/repository.py tests/test_calendar_link.py
git commit -m "feat(calendar): link/unlink service with adopt, move, and conflict"
```

---

## Task 3: Auto-link at record time

**Files:**

- Modify: `src/calendar_matcher.py`
- Modify: `src/calendar_events/reader.py`
- Modify: `src/main.py`
- Modify: `src/api/routes/reprocess.py`
- Test: `tests/test_calendar_matcher.py` (append), `tests/test_pipeline_runner.py` (append), `tests/test_api_reprocess.py` (append)

**Interfaces:**

- Produces: `src.calendar_matcher._event_uid(event_identifier: str, start_ts: float) -> str`; `CalendarMatch.event_uid: str`; `calendar_fields["calendar_event_uid"]` in `main._process_audio` and `reprocess._do_reprocess`.

- [ ] **Step 1: Write the failing matcher/parity test**

Append to `tests/test_calendar_matcher.py`:

```python
def test_event_uid_helper_matches_reader_format():
    from src.calendar_matcher import _event_uid

    # Same format the mirror reader uses: "<identifier>:<int(start_ts)>".
    assert _event_uid("E1", 1000.9) == "E1:1000"
    assert _event_uid("abc-123", 1_700_000_000.0) == "abc-123:1700000000"


def test_reader_uses_shared_event_uid_helper():
    from src.calendar_events.reader import _events_from_extracted

    out = _events_from_extracted(
        [
            {
                "event_identifier": "E9",
                "calendar_identifier": "c1",
                "title": "Sync",
                "start_ts": 2500.7,
                "end_ts": 4300.0,
                "attendees": [{"name": "A"}, {"name": "B"}],
                "join_url": "",
                "meeting_id": "",
                "is_all_day": False,
            }
        ],
        set(),
    )
    assert out[0].event_uid == "E9:2500"


def test_calendar_match_defaults_event_uid_empty():
    from src.calendar_matcher import CalendarMatch

    assert CalendarMatch(event_title="x").event_uid == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_calendar_matcher.py -k "event_uid" -v`
Expected: FAIL — `_event_uid` missing; `CalendarMatch` has no `event_uid`.

- [ ] **Step 3: Add the shared helper + field, populate in `_do_match`**

In `src/calendar_matcher.py`:

(a) Add the helper near the other module helpers (after `_extract_teams_details`, ~line 80):

```python
def _event_uid(event_identifier: str, start_ts: float) -> str:
    """Stable per-occurrence id shared with the calendar_events reader.

    EventKit's eventIdentifier is shared across recurring occurrences, so it
    is combined with the integer start timestamp.
    """
    return f"{event_identifier}:{int(start_ts)}"
```

(b) Add the field to `CalendarMatch` after `teams_meeting_id: str = ""` (line 42):

```python
    event_uid: str = ""
```

(c) In `_do_match`, compute the uid inside the per-event loop, right after `title = str(event.title() or "")` (line 263):

```python
            event_uid = _event_uid(str(event.eventIdentifier() or ""), event_start)
```

(d) Pass `event_uid=event_uid,` into BOTH `CalendarMatch(...)` constructions (Tier 1 `teams_url` at ~line 315 and Tier 2 `time_window` at ~line 332).

- [ ] **Step 4: DRY the reader onto the helper**

In `src/calendar_events/reader.py`:

(a) Extend the import (line 15-19) to include `_event_uid`:

```python
from src.calendar_matcher import (
    _event_uid,
    _extract_attendee_info,
    _extract_teams_details,
    _is_eventkit_available,
)
```

(b) Replace the inline uid at line 77 — `event_uid=f"{e['event_identifier']}:{int(start_ts)}",` — with:

```python
            event_uid=_event_uid(e["event_identifier"], start_ts),
```

- [ ] **Step 5: Run matcher/reader tests**

Run: `python3 -m pytest tests/test_calendar_matcher.py tests/test_calendar_reader.py -q`
Expected: PASS (parity tests green; existing reader dedup tests unaffected — same uids).

- [ ] **Step 6: Write the failing pipeline-persist test**

Append to `tests/test_pipeline_runner.py` (mirrors `test_auto_title_prefers_calendar_event_title`):

```python
def test_calendar_event_uid_is_persisted(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    runner = _make_runner(_make_config(tmp_path), db=bridge)

    runner.run(
        tmp_path / "a.wav",
        "m1",
        started_at=1000.0,
        calendar_fields={
            "calendar_event_title": "Weekly Sync",
            "calendar_event_uid": "EK1:1000",
        },
    )

    _drain(loop_thread)
    persist_calls = [c.kwargs for c in repo.update_meeting.call_args_list]
    assert any(c.get("calendar_event_uid") == "EK1:1000" for c in persist_calls)
```

- [ ] **Step 7: Run it to verify it passes already (plumbing test)**

Run: `python3 -m pytest tests/test_pipeline_runner.py::test_calendar_event_uid_is_persisted -v`
Expected: PASS — `calendar_fields` already flow to `update_meeting`, and `calendar_event_uid` is now a mutable column (Task 1). This test guards that path. (If it fails because the fake repo rejects the column, ensure `_make_repo` uses an `AsyncMock` — it does; no column validation there.)

- [ ] **Step 8: Wire `calendar_event_uid` into `main._process_audio`**

In `src/main.py`, inside the `calendar_fields` dict (lines 841-847), add:

```python
                "calendar_event_uid": calendar_match.event_uid,
```

- [ ] **Step 9: Write the failing reprocess re-supply test**

Append to `tests/test_api_reprocess.py` a test asserting the re-supplied `calendar_fields` carries `calendar_event_uid`. Locate the existing reprocess test that inspects `runner.run` kwargs (search for `calendar_event_title` in the file) and mirror it:

```python
async def test_reprocess_resupplies_calendar_event_uid(tmp_path, monkeypatch):
    # Reuse the module's existing reprocess harness. The stored meeting must
    # expose calendar_event_uid; assert it is re-supplied via calendar_fields.
    # (Follow the arrange/act of the neighbouring reprocess kwargs test.)
    ...
```

Implement it by copying the nearest existing "reprocess passes calendar_fields" test in that file and asserting
`captured_kwargs["calendar_fields"]["calendar_event_uid"] == "<stored uid>"`, seeding the meeting row with `calendar_event_uid` via `repo.update_meeting(mid, calendar_event_uid="EK1:1000")`.

- [ ] **Step 10: Run it to verify it fails**

Run: `python3 -m pytest tests/test_api_reprocess.py -k "calendar_event_uid" -v`
Expected: FAIL — reprocess only re-supplies `calendar_event_title`.

- [ ] **Step 11: Re-supply `calendar_event_uid` on reprocess**

In `src/api/routes/reprocess.py`, in the `calendar_fields` dict (lines 114-116), add the key:

```python
            calendar_fields={
                "calendar_event_title": getattr(meeting, "calendar_event_title", "") or "",
                "calendar_event_uid": getattr(meeting, "calendar_event_uid", "") or "",
            },
```

- [ ] **Step 12: Run the affected suites**

Run: `python3 -m pytest tests/test_calendar_matcher.py tests/test_calendar_reader.py tests/test_pipeline_runner.py tests/test_api_reprocess.py -q`
Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add src/calendar_matcher.py src/calendar_events/reader.py src/main.py src/api/routes/reprocess.py tests/test_calendar_matcher.py tests/test_pipeline_runner.py tests/test_api_reprocess.py
git commit -m "feat(calendar): auto-link recordings via matcher event_uid; survive reprocess"
```

---

## Task 4: API — link/unlink endpoints + server wiring

**Files:**

- Modify: `src/api/routes/meetings.py`
- Modify: `src/api/server.py`
- Test: `tests/test_api_calendar_link.py` (create)

**Interfaces:**

- Consumes: `link_meeting_to_event`, `unlink_meeting_from_event`, `CalendarLinkConflict` (Task 2); `CalendarEvent` (reader); `meetings_routes.init(repo, event_bus=None, calendar_event_repo=None)`.
- Produces: `PUT /api/meetings/{meeting_id}/calendar-link`, `DELETE /api/meetings/{meeting_id}/calendar-link`; WS event `{"type": "meeting.calendar_link", "meeting_id", "calendar_event_uid"}`.

- [ ] **Step 1: Write the failing API tests**

Create `tests/test_api_calendar_link.py` (mirrors `tests/test_api_calendar.py` harness):

```python
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import meetings as meetings_routes
from src.calendar_events.repository import CalendarEventRepository
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-cal-link"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _body(uid="EK1:1000"):
    return {
        "event_uid": uid,
        "title": "Quick Catch-Up",
        "start_ts": 1000.0,
        "end_ts": 2800.0,
        "attendees": [{"name": "Jamie", "email": "j@x.com"}],
        "organizer": None,
        "join_url": "https://teams.microsoft.com/l/meetup-join/x",
        "meeting_id": "19:mtg@thread.v2",
        "calendar_name": "Work",
    }


class _Events:
    def __init__(self):
        self.type = None


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "cl_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    crepo = CalendarEventRepository(db)

    emitted = []

    class Bus:
        def emit(self, event):
            emitted.append(event)

    meetings_routes.init(repo, event_bus=Bus(), calendar_event_repo=crepo)
    app = FastAPI()
    app.include_router(meetings_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "repo": repo, "emitted": emitted}
    await db.close()


@pytest.mark.asyncio
async def test_link_and_unlink_roundtrip(api):
    mid = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    with TestClient(api["app"]) as c:
        r = c.put(f"/api/meetings/{mid}/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 200
        assert r.json()["calendar_event_uid"] == "EK1:1000"
        assert r.json()["calendar_event_title"] == "Quick Catch-Up"
        assert any(e["type"] == "meeting.calendar_link" for e in api["emitted"])

        r = c.delete(f"/api/meetings/{mid}/calendar-link", headers=_headers())
        assert r.status_code == 200
        assert r.json()["calendar_event_uid"] == ""


@pytest.mark.asyncio
async def test_link_unknown_meeting_404(api):
    with TestClient(api["app"]) as c:
        r = c.put("/api/meetings/nope/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_link_conflict_409(api):
    m1 = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    m2 = await api["repo"].create_meeting(started_at=1010.0, status="complete")
    with TestClient(api["app"]) as c:
        assert c.put(f"/api/meetings/{m1}/calendar-link", json=_body(), headers=_headers()).status_code == 200
        r = c.put(f"/api/meetings/{m2}/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_link_requires_event_uid_422(api):
    mid = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    bad = _body()
    bad["event_uid"] = ""
    with TestClient(api["app"]) as c:
        r = c.put(f"/api/meetings/{mid}/calendar-link", json=bad, headers=_headers())
        assert r.status_code == 422
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_api_calendar_link.py -v`
Expected: FAIL — `init` has no `calendar_event_repo`; endpoints missing.

- [ ] **Step 3: Extend `meetings.init` + add the endpoints**

In `src/api/routes/meetings.py`:

(a) Add a module global and extend `init` (lines 22-30):

```python
# Injected at startup.
_repo = None
_event_bus = None
_calendar_event_repo = None


def init(repo, event_bus=None, calendar_event_repo=None):
    global _repo, _event_bus, _calendar_event_repo
    _repo = repo
    _event_bus = event_bus
    _calendar_event_repo = calendar_event_repo
```

(b) Add request models near the others (after `RenameMeetingRequest`, line 46):

```python
class CalendarLinkAttendee(BaseModel):
    name: str = ""
    email: str = ""


class CalendarLinkRequest(BaseModel):
    event_uid: str = Field(min_length=1, max_length=512)
    title: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    attendees: list[CalendarLinkAttendee] = Field(default_factory=list, max_length=200)
    organizer: CalendarLinkAttendee | None = None
    join_url: str = ""
    meeting_id: str = ""
    calendar_name: str = ""
```

(c) Add the endpoints BEFORE the generic `@router.patch("/api/meetings/{meeting_id}")` rename handler (i.e., insert just before line 249) so specific paths are declared first:

```python
@router.put("/api/meetings/{meeting_id}/calendar-link", summary="Link a recording to a calendar event")
async def link_meeting_calendar(meeting_id: str, body: CalendarLinkRequest):
    from src.calendar_events.reader import CalendarEvent
    from src.calendar_link import CalendarLinkConflict, link_meeting_to_event

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    event = CalendarEvent(
        event_uid=body.event_uid,
        title=body.title,
        start_ts=body.start_ts,
        end_ts=body.end_ts,
        attendees=[a.model_dump() for a in body.attendees],
        organizer=body.organizer.model_dump() if body.organizer else None,
        join_url=body.join_url,
        meeting_id=body.meeting_id,
        calendar_name=body.calendar_name,
    )
    try:
        await link_meeting_to_event(
            _repo, _calendar_event_repo, meeting, event, source="manual"
        )
    except CalendarLinkConflict as e:
        raise HTTPException(status_code=409, detail=str(e))

    if _event_bus is not None:
        _event_bus.emit(
            {
                "type": "meeting.calendar_link",
                "meeting_id": meeting_id,
                "calendar_event_uid": body.event_uid,
            }
        )
    updated = await _repo.get_meeting(meeting_id)
    return updated.to_dict()


@router.delete("/api/meetings/{meeting_id}/calendar-link", summary="Unlink a recording from its calendar event")
async def unlink_meeting_calendar(meeting_id: str):
    from src.calendar_link import unlink_meeting_from_event

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await unlink_meeting_from_event(_repo, _calendar_event_repo, meeting)
    if _event_bus is not None:
        _event_bus.emit(
            {"type": "meeting.calendar_link", "meeting_id": meeting_id, "calendar_event_uid": ""}
        )
    return {"meeting_id": meeting_id, "calendar_event_uid": ""}
```

- [ ] **Step 4: Wire the repo in `server.py`**

In `src/api/server.py`, replace line 146:

```python
        meetings_routes.init(self.repo, event_bus=self.event_bus)
```

with:

```python
        from src.calendar_events.repository import CalendarEventRepository as _CalEventRepo

        meetings_routes.init(
            self.repo,
            event_bus=self.event_bus,
            calendar_event_repo=_CalEventRepo(self.db),
        )
```

- [ ] **Step 5: Run the API tests**

Run: `python3 -m pytest tests/test_api_calendar_link.py -v`
Expected: PASS (4).

- [ ] **Step 6: Guard against route-order regressions**

Run: `python3 -m pytest tests/ -k "meetings or reprocess or resummarise" -q`
Expected: PASS — the new `/{meeting_id}/calendar-link` paths don't shadow the generic `/{meeting_id}` rename/delete.

- [ ] **Step 7: Commit**

```bash
git add src/api/routes/meetings.py src/api/server.py tests/test_api_calendar_link.py
git commit -m "feat(api): PUT/DELETE meeting calendar-link endpoints"
```

---

## Task 5: UI types + API client

**Files:**

- Modify: `ui/src/lib/types.ts`
- Modify: `ui/src/lib/api.ts`
- Test: `ui/src/lib/__tests__/api.calendarLink.test.ts` (create)

**Interfaces:**

- Produces: `Meeting.calendar_event_uid?: string`; `WSEvent` variant `meeting.calendar_link`; `linkMeetingToCalendarEvent(meetingId, event) -> Promise<Meeting>`; `unlinkMeetingFromCalendarEvent(meetingId) -> Promise<void>`.

- [ ] **Step 1: Write the failing api-client test**

Create `ui/src/lib/__tests__/api.calendarLink.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  linkMeetingToCalendarEvent,
  unlinkMeetingFromCalendarEvent,
} from "../api";
import type { CalendarEvent } from "../types";

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Quick Catch-Up",
  start_ts: 1000,
  end_ts: 2800,
  attendees: [{ name: "Jamie", email: "j@x.com" }],
  organizer: null,
  join_url: "https://teams/x",
  meeting_id: "19:mtg",
  calendar_name: "Work",
};

describe("calendar-link api", () => {
  let calls: { url: string; method?: string; body?: string }[];
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: input.toString(),
          method: init?.method,
          body: init?.body as string,
        });
        return new Response(
          JSON.stringify({ id: "m1", calendar_event_uid: "EK1:1000" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
  });

  it("PUTs the event payload to the link endpoint", async () => {
    await linkMeetingToCalendarEvent("m1", EVENT);
    expect(calls[0].url).toContain("/api/meetings/m1/calendar-link");
    expect(calls[0].method).toBe("PUT");
    expect(JSON.parse(calls[0].body!).event_uid).toBe("EK1:1000");
  });

  it("DELETEs to unlink", async () => {
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), method: init?.method });
        return new Response(null, { status: 204 });
      },
    ) as unknown as typeof fetch;
    await unlinkMeetingFromCalendarEvent("m1");
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toContain("/api/meetings/m1/calendar-link");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui && npx vitest run src/lib/__tests__/api.calendarLink.test.ts`
Expected: FAIL — functions not exported.

- [ ] **Step 3: Add the Meeting field + WSEvent variant**

In `ui/src/lib/types.ts`:

(a) Add to the `Meeting` interface (after `template_source?` line 53):

```ts
  calendar_event_uid?: string;
```

(b) Add to the `WSEvent` union (after the `meeting.renamed` variant, line 465 — note it currently ends the union with `;`):

```ts
  | { type: "meeting.renamed"; meeting_id: string; title: string }
  | { type: "meeting.calendar_link"; meeting_id: string; calendar_event_uid: string };
```

(Replace the trailing `;` on the `meeting.renamed` line with the added variant as shown.)

- [ ] **Step 4: Add the api wrappers**

In `ui/src/lib/api.ts`, after `renameMeeting` (line 422), add (uses the existing `request`/`requestRaw` helpers and the `Meeting`/`CalendarEvent` types already imported in this module):

```ts
export async function linkMeetingToCalendarEvent(
  meetingId: string,
  event: CalendarEvent,
): Promise<Meeting> {
  return request<Meeting>(
    `/api/meetings/${encodeURIComponent(meetingId)}/calendar-link`,
    { method: "PUT", body: JSON.stringify(event) },
  );
}

export async function unlinkMeetingFromCalendarEvent(
  meetingId: string,
): Promise<void> {
  await request(
    `/api/meetings/${encodeURIComponent(meetingId)}/calendar-link`,
    {
      method: "DELETE",
    },
  );
}
```

(Confirm `CalendarEvent` and `Meeting` are imported at the top of `api.ts`; if not, add them to the existing `import type { ... } from "./types"`.)

- [ ] **Step 5: Run the test + typecheck**

Run: `cd ui && npx vitest run src/lib/__tests__/api.calendarLink.test.ts && npx tsc --noEmit`
Expected: PASS + clean typecheck.

- [ ] **Step 6: Commit**

```bash
git add ui/src/lib/types.ts ui/src/lib/api.ts ui/src/lib/__tests__/api.calendarLink.test.ts
git commit -m "feat(ui): Meeting.calendar_event_uid + link/unlink api client"
```

---

## Task 6: `CalendarLinkPicker` component

**Files:**

- Create: `ui/src/components/calendar/CalendarLinkPicker.tsx`
- Test: `ui/src/components/calendar/__tests__/CalendarLinkPicker.test.tsx` (create)

**Interfaces:**

- Produces:
  ```ts
  interface LinkCandidate {
    id: string;
    label: string;
    subtitle: string;
  }
  interface CalendarLinkPickerProps {
    title: string;
    candidates: LinkCandidate[];
    emptyLabel: string;
    onPick: (id: string) => void;
    onClose: () => void;
    busy?: boolean;
  }
  export function CalendarLinkPicker(
    props: CalendarLinkPickerProps,
  ): JSX.Element;
  ```
- Note: the picker is **presentational** — the _candidate list_ (nearby unlinked events or recordings) is computed by the caller (Tasks 7/8), so this component has no data-fetching and is trivially testable.

- [ ] **Step 1: Write the failing component test**

Create `ui/src/components/calendar/__tests__/CalendarLinkPicker.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CalendarLinkPicker } from "../CalendarLinkPicker";

const CANDIDATES = [
  { id: "a", label: "Amelia Check-In", subtitle: "11:01 · 23m" },
  { id: "b", label: "Standup", subtitle: "10:03 · 28m" },
];

describe("CalendarLinkPicker", () => {
  it("lists candidates and filters by search", () => {
    render(
      <CalendarLinkPicker
        title="Link to calendar event"
        candidates={CANDIDATES}
        emptyLabel="Nothing nearby"
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText("Amelia Check-In")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "stand" },
    });
    expect(screen.queryByText("Amelia Check-In")).not.toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
  });

  it("calls onPick with the chosen id", () => {
    const onPick = vi.fn();
    render(
      <CalendarLinkPicker
        title="t"
        candidates={CANDIDATES}
        emptyLabel="e"
        onPick={onPick}
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("Standup"));
    expect(onPick).toHaveBeenCalledWith("b");
  });

  it("shows the empty label when no candidates", () => {
    render(
      <CalendarLinkPicker
        title="t"
        candidates={[]}
        emptyLabel="Nothing nearby"
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText("Nothing nearby")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/CalendarLinkPicker.test.tsx`
Expected: FAIL — component missing.

- [ ] **Step 3: Implement the picker**

Create `ui/src/components/calendar/CalendarLinkPicker.tsx`:

```tsx
import { useState } from "react";

export interface LinkCandidate {
  id: string;
  label: string;
  subtitle: string;
}

interface CalendarLinkPickerProps {
  title: string;
  candidates: LinkCandidate[];
  emptyLabel: string;
  onPick: (id: string) => void;
  onClose: () => void;
  busy?: boolean;
}

export function CalendarLinkPicker({
  title,
  candidates,
  emptyLabel,
  onPick,
  onClose,
  busy = false,
}: CalendarLinkPickerProps) {
  const [q, setQ] = useState("");
  const filtered = candidates.filter((c) =>
    c.label.toLowerCase().includes(q.trim().toLowerCase()),
  );

  return (
    <div
      className="absolute z-20 mt-1 w-64 rounded-lg border border-border bg-surface-raised p-3 shadow-lg text-xs"
      role="dialog"
      aria-label={title}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="font-medium text-text-primary">{title}</span>
        <button
          type="button"
          onClick={onClose}
          className="text-text-muted hover:text-text-secondary"
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <input
        type="text"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search…"
        className="w-full mb-2 px-2 py-1 rounded border border-border bg-surface text-text-primary"
      />
      {filtered.length === 0 ? (
        <p className="text-text-muted py-2">{emptyLabel}</p>
      ) : (
        <ul className="flex flex-col gap-0.5 max-h-56 overflow-auto">
          {filtered.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                disabled={busy}
                onClick={() => onPick(c.id)}
                className="w-full text-left px-2 py-1 rounded hover:bg-surface-hover disabled:opacity-50"
              >
                <span className="block text-text-primary truncate">
                  {c.label}
                </span>
                <span className="block text-[10px] text-text-muted">
                  {c.subtitle}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run the test**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/CalendarLinkPicker.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/CalendarLinkPicker.tsx ui/src/components/calendar/__tests__/CalendarLinkPicker.test.tsx
git commit -m "feat(ui): reusable CalendarLinkPicker"
```

---

## Task 7: Calendar collapse + EventCard annotation + link menu

**Files:**

- Modify: `ui/src/components/calendar/CalendarView.tsx`
- Modify: `ui/src/components/calendar/EventCard.tsx`
- Test: `ui/src/components/calendar/__tests__/CalendarCollapse.test.tsx` (create), `ui/src/components/calendar/__tests__/EventCard.test.tsx` (create)

**Interfaces:**

- Consumes: `linkMeetingToCalendarEvent` (Task 5), `CalendarLinkPicker` + `LinkCandidate` (Task 6), `getCalendarEvents` (existing).
- Produces: `CalendarView` filters out linked events (`visibleEvents`); `EventCard` shows a "↳ linked" annotation + a `⋯` "Link to calendar event" menu.

- [ ] **Step 1: Write the failing collapse test**

Create `ui/src/components/calendar/__tests__/CalendarCollapse.test.tsx` — assert `DayDetail` renders no dashed entry once a meeting links it. Because collapse is done in `CalendarView` (which fetches), test the pure filter by rendering `DayDetail` with pre-filtered props is not representative; instead test the filter helper. Extract the filter as an exported pure function `collapseLinkedEvents(meetings, events)` in `CalendarView.tsx` and test it:

```tsx
import { describe, it, expect } from "vitest";
import { collapseLinkedEvents } from "../CalendarView";
import type { Meeting, CalendarEvent } from "../../../lib/types";

const meeting = (over: Partial<Meeting>): Meeting =>
  ({
    id: "m1",
    title: "T",
    started_at: 1000,
    ended_at: 2000,
    duration_seconds: 1000,
    status: "complete",
    audio_path: null,
    transcript_json: null,
    summary_markdown: null,
    tags: [],
    language: null,
    word_count: null,
    label: "",
    created_at: 0,
    updated_at: 0,
    calendar_event_title: "",
    attendees_json: "[]",
    calendar_confidence: 0,
    teams_join_url: "",
    teams_meeting_id: "",
    ...over,
  }) as Meeting;

const ev = (uid: string): CalendarEvent => ({
  event_uid: uid,
  title: "E",
  start_ts: 1000,
  end_ts: 2000,
  attendees: [],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "",
});

describe("collapseLinkedEvents", () => {
  it("drops events already linked to a recording", () => {
    const out = collapseLinkedEvents(
      [meeting({ calendar_event_uid: "EK1:1000" })],
      [ev("EK1:1000"), ev("EK2:2000")],
    );
    expect(out.map((e) => e.event_uid)).toEqual(["EK2:2000"]);
  });

  it("keeps all events when nothing is linked", () => {
    const out = collapseLinkedEvents([meeting({})], [ev("EK1:1000")]);
    expect(out).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/CalendarCollapse.test.tsx`
Expected: FAIL — `collapseLinkedEvents` not exported.

- [ ] **Step 3: Add + wire the collapse filter in CalendarView**

In `ui/src/components/calendar/CalendarView.tsx`:

(a) Add the exported pure helper above the component (after `getDateRange`):

```tsx
import type { Meeting, CalendarEvent } from "../../lib/types";

export function collapseLinkedEvents(
  meetings: Meeting[],
  events: CalendarEvent[],
): CalendarEvent[] {
  const linked = new Set(
    meetings.map((m) => m.calendar_event_uid).filter((u): u is string => !!u),
  );
  return events.filter((e) => !linked.has(e.event_uid));
}
```

(b) After `const events = eventsData?.events ?? [];` (line 118), compute:

```tsx
const visibleEvents = useMemo(
  () => collapseLinkedEvents(meetings, events),
  [meetings, events],
);
```

(c) In the render, pass `events={visibleEvents}` (not `events`) to **all four** sub-views (`MonthGrid`, `WeekTimeline`, `DayDetail`, `AgendaList`) — lines 235, 244, 252, 259.

- [ ] **Step 4: Run the collapse test**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/CalendarCollapse.test.tsx`
Expected: PASS.

- [ ] **Step 5: Write the failing EventCard test**

Create `ui/src/components/calendar/__tests__/EventCard.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { EventCard } from "../EventCard";
import type { Meeting } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

const base: Meeting = {
  id: "m1",
  title: "Amelia Monthly Check-In",
  started_at: 1000,
  ended_at: 2000,
  duration_seconds: 1380,
  status: "complete",
  audio_path: null,
  transcript_json: null,
  summary_markdown: null,
  tags: [],
  language: null,
  word_count: null,
  label: "",
  created_at: 0,
  updated_at: 0,
  calendar_event_title: "",
  attendees_json: "[]",
  calendar_confidence: 0,
  teams_join_url: "",
  teams_meeting_id: "",
};

function renderCard(m: Meeting) {
  const Wrapper = makeWrapper();
  return render(
    <MemoryRouter>
      <Wrapper>
        <EventCard meeting={m} />
      </Wrapper>
    </MemoryRouter>,
  );
}

describe("EventCard link affordances", () => {
  it("shows the linked-entry annotation when linked", () => {
    renderCard({
      ...base,
      calendar_event_uid: "EK1:1000",
      calendar_event_title: "Jamie - Quick Catch-Up",
    });
    expect(screen.getByText(/Jamie - Quick Catch-Up/)).toBeInTheDocument();
  });

  it("exposes a link menu when not linked", () => {
    renderCard(base);
    fireEvent.click(screen.getByRole("button", { name: /link options/i }));
    expect(screen.getByText(/Link to calendar event/i)).toBeInTheDocument();
  });
});
```

> Note: if `makeWrapper()` already provides a Router, drop the `MemoryRouter` here. Check `ui/src/test/queryWrapper.tsx`; adjust the wrapper usage to match how sibling tests (e.g. `WeekTimeline.test.tsx`) render router-dependent components.

- [ ] **Step 6: Run it to verify it fails**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/EventCard.test.tsx`
Expected: FAIL — no annotation / no menu.

- [ ] **Step 7: Add the annotation + link menu to EventCard**

In `ui/src/components/calendar/EventCard.tsx` (full-mode branch):

(a) Imports at top:

```tsx
import { useState } from "react";
import { useQueryClient, useMutation } from "@tanstack/react-query";
import { getCalendarEvents, linkMeetingToCalendarEvent } from "../../lib/api";
import { CalendarLinkPicker, type LinkCandidate } from "./CalendarLinkPicker";
import { format } from "date-fns";
```

(b) Inside `EventCard` (full mode, before `return`), add state + the picker data + mutation:

```tsx
const qc = useQueryClient();
const [menuOpen, setMenuOpen] = useState(false);
const [pickerOpen, setPickerOpen] = useState(false);
const linked = !!meeting.calendar_event_uid;

// Nearby unlinked calendar entries (± same day) for the picker.
const anchor = meeting.started_at;
const eventsQuery = useQuery({
  queryKey: ["calendar-events", "picker", meeting.id],
  queryFn: () => getCalendarEvents(anchor - 86400, anchor + 86400),
  enabled: pickerOpen,
  staleTime: 30_000,
});
const candidates: LinkCandidate[] = (eventsQuery.data?.events ?? []).map(
  (e) => ({
    id: e.event_uid,
    label: e.title || "Untitled",
    subtitle: format(new Date(e.start_ts * 1000), "EEE HH:mm"),
  }),
);

const link = useMutation({
  mutationFn: (eventUid: string) => {
    const ev = (eventsQuery.data?.events ?? []).find(
      (e) => e.event_uid === eventUid,
    )!;
    return linkMeetingToCalendarEvent(meeting.id, ev);
  },
  onSuccess: () => {
    setPickerOpen(false);
    setMenuOpen(false);
    void qc.invalidateQueries({ queryKey: ["calendar"] });
    void qc.invalidateQueries({ queryKey: ["calendar-events"] });
    void qc.invalidateQueries({ queryKey: ["meeting", meeting.id] });
  },
});
```

Add `import { useQuery } from "@tanstack/react-query";` to the import in (a).

(c) In the full-mode markup, add a `⋯` button + annotation. Inside the outer card `<div>` (keep the existing click-to-navigate on the card), add a right-aligned menu trigger and, under the meta row, the annotation. Because the card root is itself a click target, wrap the menu button with `onClick={(e) => e.stopPropagation()}`:

```tsx
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-text-primary truncate">{title}</p>
        <div className="flex items-center gap-2 mt-0.5 text-xs text-text-muted">
          {/* ...existing duration / attendees / Teams / Prep ... */}
        </div>
        {linked && meeting.calendar_event_title && (
          <p className="mt-0.5 text-[11px] text-text-muted truncate">
            ↳ linked to {meeting.calendar_event_title}
          </p>
        )}
      </div>
      <div className="relative shrink-0" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          aria-label="link options"
          onClick={() => setMenuOpen((v) => !v)}
          className="px-1 text-text-muted hover:text-text-secondary"
        >
          ⋯
        </button>
        {menuOpen && !pickerOpen && (
          <div className="absolute right-0 z-10 mt-1 w-48 rounded-lg border border-border bg-surface-raised p-1 shadow-lg text-xs">
            {linked ? (
              <span className="block px-2 py-1 text-text-muted">Linked to a calendar event</span>
            ) : (
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="w-full text-left px-2 py-1 rounded hover:bg-surface-hover text-text-primary"
              >
                Link to calendar event
              </button>
            )}
          </div>
        )}
        {pickerOpen && (
          <CalendarLinkPicker
            title="Link to calendar event"
            candidates={candidates}
            emptyLabel="No nearby calendar entries"
            busy={link.isPending}
            onPick={(id) => link.mutate(id)}
            onClose={() => setPickerOpen(false)}
          />
        )}
      </div>
```

(Place this `<div className="relative shrink-0" …>` as a sibling of the `min-w-0 flex-1` content div, inside the card flex row.)

- [ ] **Step 8: Run EventCard test + typecheck**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/EventCard.test.tsx && npx tsc --noEmit`
Expected: PASS + clean.

- [ ] **Step 9: Commit**

```bash
git add ui/src/components/calendar/CalendarView.tsx ui/src/components/calendar/EventCard.tsx ui/src/components/calendar/__tests__/CalendarCollapse.test.tsx ui/src/components/calendar/__tests__/EventCard.test.tsx
git commit -m "feat(ui): collapse linked calendar events + EventCard link menu"
```

---

## Task 8: UpcomingEventCard — "Assign a recording"

**Files:**

- Modify: `ui/src/components/calendar/UpcomingEventCard.tsx`
- Test: `ui/src/components/calendar/__tests__/AssignRecording.test.tsx` (create)

**Interfaces:**

- Consumes: `getCalendarMeetings`, `linkMeetingToCalendarEvent` (Task 5), `CalendarLinkPicker` (Task 6).
- Produces: an "Assign a recording" action in the popover that links a chosen nearby unlinked recording to this event.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/AssignRecording.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

vi.mock("../../../hooks/useDaemonStatus", () => ({
  useDaemonStatus: () => ({
    state: "idle",
    daemonRunning: true,
    activeMeeting: null,
  }),
}));

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Quick Catch-Up",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("UpcomingEventCard assign-a-recording", () => {
  const calls: { url: string; method?: string }[] = [];
  beforeEach(() => {
    calls.length = 0;
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        calls.push({ url, method: init?.method });
        if (url.includes("/api/calendar/meetings")) {
          return new Response(
            JSON.stringify({
              meetings: [
                {
                  id: "rec1",
                  title: "Amelia Check-In",
                  started_at: 1_699_999_000,
                  ended_at: 1_700_001_000,
                  duration_seconds: 1380,
                  status: "complete",
                  tags: [],
                  calendar_event_title: "",
                  attendees_json: "[]",
                  calendar_confidence: 0,
                  teams_join_url: "",
                  teams_meeting_id: "",
                  calendar_event_uid: "",
                },
              ],
              count: 1,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ id: "rec1", calendar_event_uid: "EK1:1000" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
  });

  it("links a chosen recording to the event", async () => {
    render(<UpcomingEventCard event={EVENT} />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Quick Catch-Up/i }));
    fireEvent.click(screen.getByText(/Assign a recording/i));
    await waitFor(() =>
      expect(screen.getByText("Amelia Check-In")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("Amelia Check-In"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/meetings/rec1/calendar-link") &&
            c.method === "PUT",
        ),
      ).toBe(true),
    );
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/AssignRecording.test.tsx`
Expected: FAIL — no "Assign a recording" action.

- [ ] **Step 3: Add the action to UpcomingEventCard**

In `ui/src/components/calendar/UpcomingEventCard.tsx`:

(a) Extend imports:

```tsx
import { useQuery } from "@tanstack/react-query";
import { getCalendarMeetings, linkMeetingToCalendarEvent } from "../../lib/api";
import { CalendarLinkPicker, type LinkCandidate } from "./CalendarLinkPicker";
```

(b) Add state + query + mutation inside the component (after the existing `record` mutation):

```tsx
const [assigning, setAssigning] = useState(false);
const meetingsQuery = useQuery({
  queryKey: ["calendar", "assign-picker", event.event_uid],
  queryFn: () =>
    getCalendarMeetings(event.start_ts - 86400, event.end_ts + 86400),
  enabled: assigning,
  staleTime: 30_000,
});
const recCandidates: LinkCandidate[] = (meetingsQuery.data?.meetings ?? [])
  .filter((m) => !m.calendar_event_uid)
  .map((m) => ({
    id: m.id,
    label: m.title || "Untitled",
    subtitle: format(new Date(m.started_at * 1000), "EEE HH:mm"),
  }));
const assign = useMutation({
  mutationFn: (meetingId: string) =>
    linkMeetingToCalendarEvent(meetingId, event),
  onSuccess: () => {
    setAssigning(false);
    setOpen(false);
    void queryClient.invalidateQueries({ queryKey: ["calendar"] });
    void queryClient.invalidateQueries({ queryKey: ["calendar-events"] });
  },
  onError: () => toast.error("Failed to link the recording."),
});
```

(c) In the popover actions section (inside the `border-t` action stack, after "Record this meeting"), add:

```tsx
<button
  type="button"
  onClick={() => setAssigning(true)}
  className="text-left text-accent hover:underline"
>
  Assign a recording
</button>
```

(d) Render the picker (inside the popover `<div>` so it is positioned relative to the card), right before the popover's closing `</div>`:

```tsx
{
  assigning && (
    <CalendarLinkPicker
      title="Assign a recording"
      candidates={recCandidates}
      emptyLabel="No nearby recordings"
      busy={assign.isPending}
      onPick={(id) => assign.mutate(id)}
      onClose={() => setAssigning(false)}
    />
  );
}
```

- [ ] **Step 4: Run the test + existing UpcomingEventCard tests**

Run: `cd ui && npx vitest run src/components/calendar/__tests__/AssignRecording.test.tsx src/components/calendar/__tests__/UpcomingEventCard.test.tsx src/components/calendar/__tests__/EventActions.test.tsx`
Expected: PASS (no regression to existing popover tests).

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/UpcomingEventCard.tsx ui/src/components/calendar/__tests__/AssignRecording.test.tsx
git commit -m "feat(ui): assign a recording to a calendar entry from its popover"
```

---

## Task 9: Meeting Detail card + App.tsx invalidation

**Files:**

- Modify: `ui/src/components/meetings/MeetingDetail.tsx`
- Modify: `ui/src/App.tsx`
- Test: `ui/src/components/meetings/__tests__/MeetingDetailCalendarLink.test.tsx` (create)

**Interfaces:**

- Consumes: `linkMeetingToCalendarEvent`, `unlinkMeetingFromCalendarEvent`, `getCalendarEvents` (Task 5), `CalendarLinkPicker` (Task 6).
- Produces: a link/unlink control on the calendar card in Meeting Detail; `App.tsx` invalidates `['calendar-events']` on link + completion and handles `meeting.calendar_link`.

- [ ] **Step 1: Write the failing detail test**

Create `ui/src/components/meetings/__tests__/MeetingDetailCalendarLink.test.tsx`. Because `MeetingDetail` is a large route component that fetches by URL param, test the smallest new surface by extracting the calendar-link control into a focused subcomponent `CalendarLinkCard` (see Step 3) and testing that in isolation:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CalendarLinkCard } from "../CalendarLinkCard";
import type { Meeting } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

const base: Meeting = {
  id: "m1",
  title: "Amelia Monthly Check-In",
  started_at: 1_700_000_000,
  ended_at: 1_700_001_000,
  duration_seconds: 1380,
  status: "complete",
  audio_path: null,
  transcript_json: null,
  summary_markdown: null,
  tags: [],
  language: null,
  word_count: null,
  label: "",
  created_at: 0,
  updated_at: 0,
  calendar_event_title: "",
  attendees_json: "[]",
  calendar_confidence: 0,
  teams_join_url: "",
  teams_meeting_id: "",
};

describe("CalendarLinkCard", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 204 }),
    ) as unknown as typeof fetch;
  });

  it("shows Unlink when linked", () => {
    render(
      <CalendarLinkCard
        meeting={{
          ...base,
          calendar_event_uid: "EK1:1000",
          calendar_event_title: "Jamie - Quick Catch-Up",
        }}
      />,
      { wrapper: makeWrapper() },
    );
    expect(screen.getByText(/Jamie - Quick Catch-Up/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /unlink/i })).toBeInTheDocument();
  });

  it("shows a Link button when unlinked", () => {
    render(<CalendarLinkCard meeting={base} />, { wrapper: makeWrapper() });
    expect(
      screen.getByRole("button", { name: /link to calendar event/i }),
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingDetailCalendarLink.test.tsx`
Expected: FAIL — `CalendarLinkCard` missing.

- [ ] **Step 3: Extract `CalendarLinkCard` and use it in MeetingDetail**

Create `ui/src/components/meetings/CalendarLinkCard.tsx`:

```tsx
import { useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Meeting } from "../../lib/types";
import {
  getCalendarEvents,
  linkMeetingToCalendarEvent,
  unlinkMeetingFromCalendarEvent,
} from "../../lib/api";
import {
  CalendarLinkPicker,
  type LinkCandidate,
} from "../calendar/CalendarLinkPicker";
import { useToast } from "../common/Toast";

export function CalendarLinkCard({ meeting }: { meeting: Meeting }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [pickerOpen, setPickerOpen] = useState(false);
  const linked = !!meeting.calendar_event_uid;

  const eventsQuery = useQuery({
    queryKey: ["calendar-events", "picker", meeting.id],
    queryFn: () =>
      getCalendarEvents(meeting.started_at - 86400, meeting.started_at + 86400),
    enabled: pickerOpen,
    staleTime: 30_000,
  });
  const candidates: LinkCandidate[] = (eventsQuery.data?.events ?? []).map(
    (e) => ({
      id: e.event_uid,
      label: e.title || "Untitled",
      subtitle: format(new Date(e.start_ts * 1000), "EEE HH:mm"),
    }),
  );

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["meeting", meeting.id] });
    void qc.invalidateQueries({ queryKey: ["meetings"] });
    void qc.invalidateQueries({ queryKey: ["calendar"] });
    void qc.invalidateQueries({ queryKey: ["calendar-events"] });
  };

  const link = useMutation({
    mutationFn: (eventUid: string) => {
      const ev = (eventsQuery.data?.events ?? []).find(
        (e) => e.event_uid === eventUid,
      )!;
      return linkMeetingToCalendarEvent(meeting.id, ev);
    },
    onSuccess: () => {
      setPickerOpen(false);
      invalidate();
    },
    onError: () => toast.error("Failed to link."),
  });
  const unlink = useMutation({
    mutationFn: () => unlinkMeetingFromCalendarEvent(meeting.id),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to unlink."),
  });

  return (
    <div className="relative mt-3 rounded-lg bg-surface-raised border border-border p-3 text-xs">
      {linked ? (
        <div className="flex items-center justify-between gap-2">
          <span className="text-text-primary truncate">
            Linked to {meeting.calendar_event_title || "a calendar event"}
          </span>
          <button
            type="button"
            onClick={() => unlink.mutate()}
            disabled={unlink.isPending}
            className="text-text-muted hover:text-text-secondary shrink-0"
          >
            Unlink
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          className="text-accent hover:underline"
        >
          Link to calendar event
        </button>
      )}
      {pickerOpen && (
        <CalendarLinkPicker
          title="Link to calendar event"
          candidates={candidates}
          emptyLabel="No nearby calendar entries"
          busy={link.isPending}
          onPick={(id) => link.mutate(id)}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
```

In `ui/src/components/meetings/MeetingDetail.tsx`, render `<CalendarLinkCard meeting={meeting} />` just below the existing calendar-info block (after line 660's closing `)}`), and add the import:

```tsx
import { CalendarLinkCard } from "./CalendarLinkCard";
```

(The existing `meeting.calendar_event_title` card stays as the read-only match display; `CalendarLinkCard` adds the link/unlink control below it.)

- [ ] **Step 4: Run the detail test**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingDetailCalendarLink.test.tsx`
Expected: PASS.

- [ ] **Step 5: Wire App.tsx invalidation**

In `ui/src/App.tsx`, update the invalidation block (lines 92-105):

(a) Add `meeting.calendar_link` to the condition:

```tsx
if (
  event.type === "pipeline.complete" ||
  event.type === "meeting.resummarise" ||
  event.type === "meeting.renamed" ||
  event.type === "meeting.calendar_link"
) {
  queryClient.invalidateQueries({ queryKey: ["meetings"] });
  queryClient.invalidateQueries({ queryKey: ["calendar"] });
  queryClient.invalidateQueries({ queryKey: ["calendar-events"] });
  queryClient.invalidateQueries({ queryKey: ["calendar-heatmap"] });
  if (event.meeting_id) {
    queryClient.invalidateQueries({ queryKey: ["meeting", event.meeting_id] });
  }
}
```

(The added `['calendar-events']` line also fixes the pre-existing gap where an auto-linked recording's calendar entry did not refresh on `pipeline.complete`.)

- [ ] **Step 6: Full UI test + typecheck**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: PASS + clean.

- [ ] **Step 7: Commit**

```bash
git add ui/src/components/meetings/MeetingDetail.tsx ui/src/components/meetings/CalendarLinkCard.tsx ui/src/components/meetings/__tests__/MeetingDetailCalendarLink.test.tsx ui/src/App.tsx
git commit -m "feat(ui): link/unlink calendar event from Meeting Detail; refresh calendar-events"
```

---

## Final verification

- [ ] **Python suite + lint**

Run:

```bash
python3 -m pytest tests/ -q
ruff check src/ tests/
```

Expected: full suite green (~1180+ tests), ruff clean.

- [ ] **UI suite + typecheck**

Run:

```bash
cd ui && npm test && npx tsc --noEmit
```

Expected: green + clean.

- [ ] **Manual smoke (documented, not automated)**

With a signed local build/daemon: open Calendar → Day; a recorded meeting shows a `⋯` → "Link to calendar event" → pick the entry → the dashed entry collapses and the recorded card shows "↳ linked to …". From the dashed entry popover, "Assign a recording" links the reverse way. Meeting Detail shows "Linked to … / Unlink". Reprocess preserves the link.

---

## Self-review (author checklist — completed)

**Spec coverage:** §1 data model → Task 1; §2 link service (adopt/move/409/unlink) → Task 2; §3 auto-link → Task 3; §4 API → Task 4; §5 UI (types/api → Task 5; picker → Task 6; collapse+EventCard → Task 7; UpcomingEventCard → Task 8; Detail card + App invalidation → Task 9); §6 testing folded into each task; §7 efficiency preserved (no new EventKit reads on link; client-side collapse; no reprocess).

**Deviations from spec (intentional, lower-risk):** auto-link persists the **forward link only** (no reverse mirror write inside the pipeline thread) — the forward link fully drives UI collapse; the reverse link is written on the manual path. Conflict detection uses the **forward link** (`meeting_id_for_calendar_event`) rather than the mirror, so it is correct even when the event is not yet synced.

**Placeholder scan:** the only prose-only step is Task 3 Step 9 (reprocess re-supply test), which instructs copying the file's existing reprocess-kwargs test and asserting one extra key — acceptable because the exact neighbour test is in-repo and named; all code-bearing steps show full code.

**Type consistency:** `calendar_event_uid` (snake_case) across DB/repo/API; `CalendarLinkPicker`/`LinkCandidate`, `linkMeetingToCalendarEvent`/`unlinkMeetingFromCalendarEvent`, `collapseLinkedEvents`, `CalendarLinkCard`, `_event_uid`, `CalendarLinkConflict`, `link_meeting_to_event`/`unlink_meeting_from_event` are used identically in every referencing task.
