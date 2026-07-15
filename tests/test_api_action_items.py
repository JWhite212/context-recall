"""Tests for src/api/routes/action_items.py — action item CRUD + PATCH tag override."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.action_items.repository import ActionItemRepository
from src.api.routes import action_items as action_items_routes
from src.db.database import Database
from src.db.repository import MeetingRepository


@pytest.fixture
async def action_items_client(tmp_path):
    db = Database(db_path=tmp_path / "action_items_api.db")
    await db.connect()
    repo = ActionItemRepository(db)
    meeting_repo = MeetingRepository(db)
    action_items_routes.init(repo)

    app = FastAPI()
    app.include_router(action_items_routes.router)
    with TestClient(app) as client:
        yield client, repo, meeting_repo
    await db.close()


@pytest.mark.asyncio
async def test_patch_sets_client_project_and_marks_manual(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    item_id = await repo.create(meeting_id=meeting_id, title="Do it", source="extracted")

    resp = client.patch(f"/api/action-items/{item_id}", json={"client_id": "c9"})
    assert resp.status_code == 200
    assert resp.json()["client_id"] == "c9"

    item = await repo.get(item_id)
    assert item["client_id"] == "c9"
    assert item["tag_source"] == "manual"


@pytest.mark.asyncio
async def test_patch_without_tag_fields_leaves_tag_source_untouched(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    item_id = await repo.create(meeting_id=meeting_id, title="Do it", source="extracted")

    resp = client.patch(f"/api/action-items/{item_id}", json={"status": "in_progress"})
    assert resp.status_code == 200

    item = await repo.get(item_id)
    assert item["status"] == "in_progress"
    assert item["tag_source"] == "inherited"


@pytest.mark.asyncio
async def test_patch_missing_item_404s(action_items_client):
    client, _repo, _meeting_repo = action_items_client
    resp = client.patch("/api/action-items/nope", json={"client_id": "c9"})
    assert resp.status_code == 404
