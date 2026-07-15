"""Tests for src/api/routes/reprocess.py — async submission + pipeline wiring.

Bug C4 still holds: the endpoint returns 202 immediately and the work
runs as a background asyncio task. The pipeline stages themselves now
live in src/pipeline_runner.py (tested in test_pipeline_runner.py);
these tests pin the ROUTE's responsibilities: request validation, the
reprocess-job row lifecycle, the inputs handed to the shared runner
(stored attendees, surviving mic source WAV, preserve_mappings, the
previous Notion page id), and the event-bus adapter.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import reprocess as reprocess_routes
from src.pipeline_runner import RunResult
from src.utils.config import AppConfig

TEST_TOKEN = "test-token-for-reprocess-tests"


def _make_app(repo, event_bus=None, db=None) -> FastAPI:
    reprocess_routes.init(repo, event_bus, db=db)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(reprocess_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


@pytest.fixture(autouse=True)
def _default_config(tmp_path):
    """Never read the developer's real config.yaml from route tests."""
    config = AppConfig()
    config.audio.temp_audio_dir = str(tmp_path / "temp_audio")
    with patch("src.api.routes.reprocess.load_config", return_value=config):
        yield config


class _InFlightTracker:
    """Stand-in for the DB-backed reprocess_jobs table used by tests."""

    def __init__(self) -> None:
        self._set: set[str] = set()

    def install(self, repo) -> None:
        async def _add(mid: str) -> None:
            self._set.add(mid)

        async def _complete(mid: str) -> None:
            self._set.discard(mid)

        async def _is_in_flight(mid: str) -> bool:
            return mid in self._set

        repo.add_reprocess_job = AsyncMock(side_effect=_add)
        repo.complete_reprocess_job = AsyncMock(side_effect=_complete)
        repo.is_reprocess_in_flight = AsyncMock(side_effect=_is_in_flight)

    def __contains__(self, item: str) -> bool:
        return item in self._set

    def add(self, mid: str) -> None:
        self._set.add(mid)


def _make_meeting(meeting_id="m1", audio_path="/tmp/x.wav", **overrides):
    m = MagicMock()
    m.id = meeting_id
    m.audio_path = audio_path
    m.started_at = 1000.0
    m.attendees_json = overrides.get("attendees_json", "[]")
    m.notion_page_id = overrides.get("notion_page_id", "")
    m.title_source = overrides.get("title_source", "auto")
    m.calendar_event_title = overrides.get("calendar_event_title", "")
    return m


def _make_repo(meeting=None):
    """Create a MagicMock repo with the reprocess_jobs methods wired up."""
    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=meeting)
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()
    tracker = _InFlightTracker()
    tracker.install(repo)
    repo._in_flight = tracker  # convenience handle for tests
    return repo


class FakeRunner:
    """Records what the route asks the shared pipeline to do."""

    def __init__(self, behaviour=None):
        self.calls: list[tuple[tuple, dict]] = []
        self.behaviour = behaviour  # callable(fake, args, kwargs) -> RunResult
        self.config = None
        self.emit = None
        self.bridge = None

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.behaviour:
            return self.behaviour(self, args, kwargs)
        return RunResult("complete", title="Test Meeting")


def _patch_runner(fake: FakeRunner):
    def factory(config, emit, bridge):
        fake.config = config
        fake.emit = emit
        fake.bridge = bridge
        return fake

    return patch("src.api.routes.reprocess._make_runner", side_effect=factory)


def _wait_for_drain(repo, meeting_id="m1", timeout=3.0):
    deadline = time.monotonic() + timeout
    while meeting_id in repo._in_flight and time.monotonic() < deadline:
        time.sleep(0.05)


def test_reprocess_returns_202_immediately_even_for_slow_pipelines(tmp_path):
    """The endpoint must return 202 Accepted within milliseconds, even
    when the underlying pipeline would take many seconds (Bug C4)."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

    def slow(fake, args, kwargs):
        time.sleep(5)
        return RunResult("complete", title="Slow Meeting")

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(FakeRunner(behaviour=slow)):
            start = time.monotonic()
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            elapsed = time.monotonic() - start

        assert resp.status_code == 202, (
            f"expected 202 Accepted; got {resp.status_code}: {resp.text}"
        )
        assert elapsed < 1.0, (
            f"endpoint returned in {elapsed:.2f}s; the 5s slow pipeline must run "
            "in the background, not in the HTTP request"
        )
        body = resp.json()
        assert body["meeting_id"] == "m1"
        assert body["status"] == "accepted"


def test_reprocess_409_when_already_in_flight(tmp_path):
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    repo._in_flight.add("m1")

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 409


def test_reprocess_404_when_meeting_missing():
    repo = _make_repo(meeting=None)

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/missing/reprocess", headers=_auth_headers())
        assert resp.status_code == 404


def test_reprocess_400_when_no_audio_file(tmp_path):
    """Audio path on the row but the file no longer exists on disk."""
    repo = _make_repo(meeting=_make_meeting(audio_path="/no/such/file.wav"))

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 400


def test_runner_receives_full_reprocess_context(tmp_path, _default_config):
    """The route must hand the shared runner everything the live path
    would have had: stored attendees, the surviving mic source WAV,
    preserve_mappings, the previous Notion page id, and is_reprocess."""
    temp_dir = tmp_path / "temp_audio"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "meeting_20260708_120000_mic.wav").write_bytes(b"\x00" * 100)

    audio_file = tmp_path / "meeting_20260708_120000.wav"
    audio_file.write_bytes(b"\x00" * 100)

    meeting = _make_meeting(
        audio_path=str(audio_file),
        attendees_json='[{"name": "Sarah Chen", "email": "s@x.com"}]',
        notion_page_id="old-page",
    )
    repo = _make_repo(meeting=meeting)
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    assert len(fake.calls) == 1
    args, kwargs = fake.calls[0]
    assert str(args[0]) == str(audio_file)
    assert args[1] == "m1"
    assert args[2] == 1000.0
    assert kwargs["attendees"] == [{"name": "Sarah Chen", "email": "s@x.com"}]
    assert kwargs["mic_audio_path"] == temp_dir / "meeting_20260708_120000_mic.wav"
    assert kwargs["preserve_mappings"] is True
    assert kwargs["notion_page_id"] == "old-page"
    assert kwargs["is_reprocess"] is True
    assert kwargs["preserve_title"] is False


def test_runner_keeps_calendar_auto_title_on_reprocess(tmp_path, _default_config):
    """I2: a reprocess must pass the stored calendar title into the runner
    so an auto-titled meeting keeps its calendar name instead of reverting
    to the fresh summary.title."""
    audio_file = tmp_path / "meeting_20260708_120000.wav"
    audio_file.write_bytes(b"\x00" * 100)

    meeting = _make_meeting(audio_path=str(audio_file), calendar_event_title="Weekly Sync")
    repo = _make_repo(meeting=meeting)
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    _, kwargs = fake.calls[0]
    assert kwargs["calendar_fields"] == {"calendar_event_title": "Weekly Sync"}


def test_runner_gets_empty_calendar_title_when_never_matched(tmp_path, _default_config):
    """A meeting with no stored calendar match passes an empty title, so
    the runner falls back to summary.title."""
    audio_file = tmp_path / "meeting_20260708_120000.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    _, kwargs = fake.calls[0]
    assert kwargs["calendar_fields"] == {"calendar_event_title": ""}


def test_runner_preserves_manual_title_on_reprocess(tmp_path, _default_config):
    """A user-renamed meeting (title_source == 'manual') must not have
    its title reverted by a reprocess run."""
    audio_file = tmp_path / "meeting_20260708_120000.wav"
    audio_file.write_bytes(b"\x00" * 100)

    meeting = _make_meeting(audio_path=str(audio_file), title_source="manual")
    repo = _make_repo(meeting=meeting)
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    _, kwargs = fake.calls[0]
    assert kwargs["preserve_title"] is True


def test_runner_gets_no_mic_path_when_sources_swept(tmp_path):
    """Once the temp-retention sweep removed the source WAVs, the route
    passes mic_audio_path=None and the runner degrades gracefully."""
    audio_file = tmp_path / "meeting_20260708_120000.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    _, kwargs = fake.calls[0]
    assert kwargs["mic_audio_path"] is None


def test_background_task_bridge_reaches_repo_and_job_clears(tmp_path):
    """End-to-end through the route's DbBridge: a runner writing through
    the bridge must land on the repo, and the job row must clear."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

    def write_complete(fake, args, kwargs):
        fake.bridge.update_meeting(args[1], status="complete")
        return RunResult("complete", title="Bridged")

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(FakeRunner(behaviour=write_complete)):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                statuses = [
                    call.kwargs.get("status")
                    for call in repo.update_meeting.await_args_list
                    if "status" in call.kwargs
                ]
                if "complete" in statuses:
                    break
                time.sleep(0.05)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert statuses[0] == "transcribing", "route must mark in-flight synchronously"
    assert "complete" in statuses
    assert "m1" not in repo._in_flight
    repo.complete_reprocess_job.assert_awaited_with("m1")


def test_runner_crash_marks_error_and_clears_job(tmp_path):
    """An unexpected crash inside the runner must not leave the row in
    'transcribing' or the job marker set."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    bus = MagicMock()

    def crash(fake, args, kwargs):
        raise RuntimeError("unexpected pipeline crash")

    app = _make_app(repo, event_bus=bus)
    with TestClient(app) as c:
        with _patch_runner(FakeRunner(behaviour=crash)):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert "error" in statuses
    assert "m1" not in repo._in_flight
    error_events = [
        c.args[0]
        for c in bus.emit.call_args_list
        if c.args and c.args[0].get("type") == "pipeline.error"
    ]
    assert error_events and "unexpected pipeline crash" in error_events[0]["error"]


def test_runner_events_flow_through_event_bus_adapter(tmp_path):
    """Events the runner emits (orchestrator-style emit(type, **kw)) must
    arrive on the event bus as the dict shape the UI expects."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    bus = MagicMock()

    def emit_complete(fake, args, kwargs):
        fake.emit("pipeline.complete", meeting_id=args[1], title="Adapted")
        return RunResult("complete", title="Adapted")

    app = _make_app(repo, event_bus=bus)
    with TestClient(app) as c:
        with _patch_runner(FakeRunner(behaviour=emit_complete)):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    complete_events = [
        c.args[0]
        for c in bus.emit.call_args_list
        if c.args and c.args[0].get("type") == "pipeline.complete"
    ]
    assert complete_events == [
        {"type": "pipeline.complete", "meeting_id": "m1", "title": "Adapted"}
    ]


def test_malformed_attendees_json_degrades_to_empty_list(tmp_path):
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    meeting = _make_meeting(audio_path=str(audio_file), attendees_json="{not json")
    repo = _make_repo(meeting=meeting)
    fake = FakeRunner()

    app = _make_app(repo)
    with TestClient(app) as c:
        with _patch_runner(fake):
            resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
            assert resp.status_code == 202
            _wait_for_drain(repo)

    _, kwargs = fake.calls[0]
    assert kwargs["attendees"] == []
