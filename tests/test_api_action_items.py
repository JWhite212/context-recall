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
async def test_patch_explicit_null_clears_client_and_marks_manual(action_items_client):
    """PATCH {"client_id": null} must untag the item (clearing is a manual
    action, so tag_source flips to 'manual'). exclude_none would silently
    drop the null and make untagging a no-op."""
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    item_id = await repo.create(
        meeting_id=meeting_id, title="Do it", source="extracted", client_id="c9"
    )

    resp = client.patch(f"/api/action-items/{item_id}", json={"client_id": None})
    assert resp.status_code == 200
    assert resp.json()["client_id"] is None

    item = await repo.get(item_id)
    assert item["client_id"] is None
    assert item["tag_source"] == "manual"


@pytest.mark.asyncio
async def test_patch_explicit_null_clears_project(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    item_id = await repo.create(
        meeting_id=meeting_id, title="Do it", source="extracted", project_id="p1"
    )

    resp = client.patch(f"/api/action-items/{item_id}", json={"project_id": None})
    assert resp.status_code == 200

    item = await repo.get(item_id)
    assert item["project_id"] is None
    assert item["tag_source"] == "manual"


@pytest.mark.asyncio
async def test_patch_omitted_fields_are_not_cleared(action_items_client):
    """exclude_unset semantics: a field absent from the body stays put —
    only an explicit null clears it."""
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    item_id = await repo.create(
        meeting_id=meeting_id, title="Do it", source="extracted", client_id="c9", assignee="Sam"
    )

    resp = client.patch(f"/api/action-items/{item_id}", json={"priority": "high"})
    assert resp.status_code == 200

    item = await repo.get(item_id)
    assert item["client_id"] == "c9"
    assert item["assignee"] == "Sam"
    assert item["priority"] == "high"


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


@pytest.mark.asyncio
async def test_list_endpoint_filters_by_project(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    await repo.create(meeting_id=meeting_id, title="x", project_id="p1")
    await repo.create(meeting_id=meeting_id, title="y", project_id="p2")
    resp = client.get("/api/action-items?project_id=p1")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["items"]] == ["x"]


@pytest.mark.asyncio
async def test_list_endpoint_filters_by_client_and_priority(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    await repo.create(meeting_id=meeting_id, title="a", priority="high", client_id="c1")
    await repo.create(meeting_id=meeting_id, title="b", priority="low", client_id="c1")
    resp = client.get("/api/action-items?client_id=c1&priority=high")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["items"]] == ["a"]


@pytest.mark.asyncio
async def test_list_endpoint_filters_by_due_after(action_items_client):
    client, repo, meeting_repo = action_items_client
    meeting_id = await meeting_repo.create_meeting(started_at=1700000000)
    await repo.create(meeting_id=meeting_id, title="future", due_date="2026-08-01")
    await repo.create(meeting_id=meeting_id, title="past", due_date="2026-01-01")
    resp = client.get("/api/action-items?due_after=2026-06-01")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["items"]] == ["future"]
