import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import prep as prep_routes
from src.db.database import Database
from src.prep.repository import PrepRepository

TEST_TOKEN = "test-token-for-prep"


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
    db = Database(db_path=tmp_path / "prep_api.db")
    await db.connect()
    repo = PrepRepository(db)
    await repo.create(
        content_markdown="brief",
        calendar_event_uid="EK1:1000",
        event_signature="sig",
        expires_at=time.time() + 3600,
    )
    prep_routes.init(repo)
    app = FastAPI()
    app.include_router(prep_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db}
    await db.close()


@pytest.mark.asyncio
async def test_upcoming_list(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/upcoming-list", headers=_headers())
        assert r.status_code == 200
        assert [b["calendar_event_uid"] for b in r.json()] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_prepared_events(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/prepared-events", headers=_headers())
        assert r.status_code == 200
        assert r.json() == {"event_uids": ["EK1:1000"]}
