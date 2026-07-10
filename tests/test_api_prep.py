import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.action_items.repository import ActionItemRepository
from src.api.auth import verify_token
from src.api.routes import prep as prep_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository
from src.series.repository import SeriesRepository
from src.utils.config import PrepConfig, SummarisationConfig

TEST_TOKEN = "test-token-for-prep-events"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "prep_evt_api.db")
    await db.connect()
    repo = PrepRepository(db)
    gen = PrepBriefingGenerator(
        config=PrepConfig(),
        summarisation_config=SummarisationConfig(),
        meeting_repo=MeetingRepository(db),
        action_item_repo=ActionItemRepository(db),
        series_repo=SeriesRepository(db),
        prep_repo=repo,
    )
    gen._summariser.chat = lambda system, user: f"stub briefing\n\n{user}"
    prep_routes.init(repo, gen)
    app = FastAPI()
    app.include_router(prep_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo}
    await db.close()


def _body(uid="EK1:1000"):
    return {
        "event_uid": uid,
        "title": "Weekly sync",
        "attendees": [{"name": "Alice", "email": "a@x.com"}],
        "attendee_names": ["Alice"],
        "end_ts": time.time() + 3600,
        "series_id": None,
    }


@pytest.mark.asyncio
async def test_by_event_get_204_when_none(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/by-event/NOPE:0", headers=_headers())
        assert r.status_code == 204


@pytest.mark.asyncio
async def test_by_event_generate_then_get(api):
    with TestClient(api["app"]) as c:
        gen = c.post("/api/prep/by-event/generate", headers=_headers(), json=_body())
        assert gen.status_code == 201
        assert gen.json()["calendar_event_uid"] == "EK1:1000"
        assert "stub briefing" in gen.json()["content_markdown"]
        got = c.get("/api/prep/by-event/EK1:1000", headers=_headers())
        assert got.status_code == 200
        assert got.json()["event_signature"]  # signature was computed + stored


@pytest.mark.asyncio
async def test_by_event_generate_is_not_captured_by_meeting_id_route(api):
    # Proves POST /by-event/generate precedes POST /{meeting_id}/generate:
    # if captured, meeting_id="by-event" with empty context would still 201 but
    # WITHOUT a calendar_event_uid. Assert the link is present.
    with TestClient(api["app"]) as c:
        r = c.post("/api/prep/by-event/generate", headers=_headers(), json=_body("EK2:2000"))
        assert r.status_code == 201
        assert r.json()["calendar_event_uid"] == "EK2:2000"


@pytest.mark.asyncio
async def test_by_event_regenerate_returns_newest(api):
    with TestClient(api["app"]) as c:
        c.post("/api/prep/by-event/generate", headers=_headers(), json=_body("EK3:3000"))
        b = _body("EK3:3000")  # regenerate with a changed title
        b["title"] = "Renamed"
        c.post("/api/prep/by-event/generate", headers=_headers(), json=b)
        got = c.get("/api/prep/by-event/EK3:3000", headers=_headers())
        assert got.status_code == 200
        # newest wins (both rows share the uid; get_by_calendar_event orders by generated_at DESC)
        assert "Renamed" in got.json()["content_markdown"]
