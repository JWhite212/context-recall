import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import automations as auto_routes
from src.automations.repository import AutomationRepository
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-for-automations"


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
    db = Database(db_path=tmp_path / "auto_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    auto_repo = AutomationRepository(db)
    auto_routes.init(repo, auto_repo)
    app = FastAPI()
    app.include_router(auto_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "auto_repo": auto_repo}
    await db.close()


@pytest.mark.asyncio
async def test_rule_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/automation-rules",
            headers=_auth_headers(),
            json={
                "name": "R",
                "match_mode": "all",
                "conditions": [{"field": "tag", "value": "Type/Discovery"}],
                "actions": [{"type": "apply_tag", "tags": ["Reviewed"]}],
            },
        )
        assert created.status_code == 201
        rid = created.json()["id"]
        patched = c.patch(
            f"/api/automation-rules/{rid}", headers=_auth_headers(), json={"enabled": False}
        )
        assert patched.json()["enabled"] is False
        assert c.delete(f"/api/automation-rules/{rid}", headers=_auth_headers()).status_code == 200
        assert c.get("/api/automation-rules", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_create_rejects_empty_conditions(api):
    with TestClient(api["app"]) as c:
        r = c.post(
            "/api/automation-rules",
            headers=_auth_headers(),
            json={"name": "R", "conditions": [], "actions": [{"type": "notify"}]},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_meeting_automations_404_for_unknown_meeting(api):
    with TestClient(api["app"]) as c:
        assert c.get("/api/meetings/nope/automations", headers=_auth_headers()).status_code == 404
