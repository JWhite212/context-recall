"""Tests for src/api/routes/export.py — meeting export endpoint."""

import json
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import export as export_routes
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-for-export-tests"


def _make_app(repo: MeetingRepository) -> FastAPI:
    export_routes.init(repo)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(export_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def client(db: Database):
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    repo = MeetingRepository(db)
    app = _make_app(repo)
    with TestClient(app) as c:
        yield c, repo
    auth_mod._auth_token = original


@pytest.mark.asyncio
async def test_export_markdown_format(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(
        mid,
        title="Sprint Planning",
        summary_markdown="## Summary\nWe planned the sprint.",
        tags=["planning"],
        status="complete",
        duration_seconds=1800.0,
    )

    resp = c.post(f"/api/export/{mid}?format=markdown", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert "---" in body  # YAML frontmatter delimiters
    assert "Sprint Planning" in body
    assert "type: meeting-note" in body


@pytest.mark.asyncio
async def test_export_markdown_with_transcript(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())

    transcript_json = json.dumps(
        [
            {"start": 0, "end": 5, "text": "Hello everyone.", "speaker": "Me"},
            {"start": 5, "end": 10, "text": "Let's begin.", "speaker": "Remote"},
        ]
    )

    await repo.update_meeting(
        mid,
        title="Team Sync",
        transcript_json=transcript_json,
        status="complete",
        duration_seconds=600.0,
    )

    resp = c.post(f"/api/export/{mid}?format=markdown", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.text
    assert "## Full Transcript" in body
    assert "[00:00:00]" in body
    assert "[Me]" in body
    assert "[00:00:05]" in body


@pytest.mark.asyncio
async def test_export_json_format(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Design Review", status="complete")

    resp = c.post(f"/api/export/{mid}?format=json", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == mid
    assert data["title"] == "Design Review"


@pytest.mark.asyncio
async def test_export_invalid_format_rejected(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())

    resp = c.post(f"/api/export/{mid}?format=pdf", headers=_auth_headers())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_export_filename_sanitized(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Test", status="complete")

    resp = c.post(f"/api/export/{mid}?format=markdown", headers=_auth_headers())
    assert resp.status_code == 200
    disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert ".md" in disposition
    # Verify the filename doesn't contain dangerous characters.
    # The meeting ID is a UUID, so it should be safe already.
    assert "/" not in disposition.split("filename=")[1]


@pytest.mark.asyncio
async def test_export_meeting_not_found(client):
    c, _ = client
    resp = c.post("/api/export/nonexistent-id?format=markdown", headers=_auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_markdown_includes_insights_when_repo_wired(db: Database):
    """When an InsightRepository is injected, the export appends an Insights
    section built from its stored results (fetched live, since insights are
    extracted after the pipeline's own markdown write)."""
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        repo = MeetingRepository(db)
        mid = await repo.create_meeting(started_at=time.time())
        await repo.update_meeting(mid, title="Client Call", status="complete")

        class FakeInsightRepo:
            async def results_for_meeting(self, meeting_id):
                assert meeting_id == mid
                return [
                    {"definition_name": "Questions", "content": "Is it live?", "fields": None},
                ]

        export_routes.init(repo, FakeInsightRepo())
        app = FastAPI()
        auth_deps = [Depends(verify_token)]
        app.include_router(export_routes.router, dependencies=auth_deps)

        with TestClient(app) as c:
            resp = c.post(f"/api/export/{mid}?format=markdown", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.text
        assert "## Insights" in body
        assert "Questions" in body
        assert "Is it live?" in body
    finally:
        auth_mod._auth_token = original
        export_routes.init(repo)  # restore module state for subsequent tests


@pytest.mark.asyncio
async def test_export_markdown_omits_insights_section_when_none_stored(client):
    """No insights recorded for the meeting -> no Insights section, even
    with the repo wired up."""
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Quiet Meeting", status="complete")

    class EmptyInsightRepo:
        async def results_for_meeting(self, meeting_id):
            return []

    export_routes.init(repo, EmptyInsightRepo())
    try:
        resp = c.post(f"/api/export/{mid}?format=markdown", headers=_auth_headers())
        assert resp.status_code == 200
        assert "## Insights" not in resp.text
    finally:
        export_routes.init(repo)  # restore module state for subsequent tests
