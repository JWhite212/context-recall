"""Tests for src/api/routes/insights.py — definition CRUD + meeting results."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import insights as insights_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.insights.repository import InsightRepository

TEST_TOKEN = "test-token-for-insights"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "insights_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    insight_repo = InsightRepository(db)
    insights_routes.init(repo, insight_repo)
    app = FastAPI()
    app.include_router(insights_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "insight_repo": insight_repo}
    await db.close()


@pytest.mark.asyncio
async def test_insight_definition_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/insight-definitions",
            headers=_auth_headers(),
            json={"name": "Risks", "prompt": "List risks."},
        )
        assert created.status_code == 201
        did = created.json()["id"]
        patched = c.patch(
            f"/api/insight-definitions/{did}",
            headers=_auth_headers(),
            json={"enabled": False},
        )
        assert patched.json()["enabled"] is False
        deleted = c.delete(f"/api/insight-definitions/{did}", headers=_auth_headers())
        assert deleted.status_code == 200
        assert c.get("/api/insight-definitions", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_meeting_insights_404_for_unknown_meeting(api):
    with TestClient(api["app"]) as c:
        resp = c.get("/api/meetings/nope/insights", headers=_auth_headers())
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_meeting_insights_returns_results(api):
    repo = api["repo"]
    insight_repo = api["insight_repo"]
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    did = await insight_repo.create(name="Risks", prompt="p")
    await insight_repo.replace_results_for_meeting(
        mid,
        [{"definition_id": did, "definition_name": "Risks", "content": "a", "speaker": ""}],
    )
    with TestClient(api["app"]) as c:
        resp = c.get(f"/api/meetings/{mid}/insights", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["definition_name"] == "Risks"
