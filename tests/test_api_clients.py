"""Tests for src/api/routes/clients.py — clients/projects CRUD + assignment."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import clients as clients_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.tagging.repository import ClientProjectRepository

TEST_TOKEN = "test-token-for-clients-tests"


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
    db = Database(db_path=tmp_path / "clients_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    cp_repo = ClientProjectRepository(db)
    clients_routes.init(repo, cp_repo)

    app = FastAPI()
    app.include_router(clients_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "cp_repo": cp_repo}
    await db.close()


@pytest.mark.asyncio
async def test_client_and_project_lifecycle(api):
    with TestClient(api["app"]) as c:
        client = c.post(
            "/api/clients",
            headers=_auth_headers(),
            json={
                "name": "Acme Corp",
                "description": "Widgets client",
                "aliases": ["Acme"],
                "email_domains": ["acme.com"],
            },
        )
        assert client.status_code == 201
        client_id = client.json()["id"]
        assert client.json()["email_domains"] == ["acme.com"]

        project = c.post(
            "/api/projects",
            headers=_auth_headers(),
            json={"name": "Portal", "client_id": client_id, "description": "Rebuild"},
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        listed = c.get(f"/api/projects?client_id={client_id}", headers=_auth_headers())
        assert [p["id"] for p in listed.json()] == [project_id]

        patched = c.patch(
            f"/api/clients/{client_id}",
            headers=_auth_headers(),
            json={"description": "Bigger widgets"},
        )
        assert patched.json()["description"] == "Bigger widgets"

        archived = c.patch(
            f"/api/projects/{project_id}",
            headers=_auth_headers(),
            json={"status": "archived"},
        )
        assert archived.status_code == 200
        assert c.get("/api/projects", headers=_auth_headers()).json() == []

        assert c.delete(f"/api/clients/{client_id}", headers=_auth_headers()).status_code == 200


@pytest.mark.asyncio
async def test_project_with_unknown_client_404(api):
    with TestClient(api["app"]) as c:
        resp = c.post(
            "/api/projects",
            headers=_auth_headers(),
            json={"name": "Orphan", "client_id": "nope"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_manual_assignment_sets_source_and_implies_client(api):
    repo = api["repo"]
    cp_repo = api["cp_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    client_id = await cp_repo.create_client(name="Acme")
    project_id = await cp_repo.create_project(name="Portal", client_id=client_id)

    with TestClient(api["app"]) as c:
        resp = c.patch(
            f"/api/meetings/{meeting_id}/assignment",
            headers=_auth_headers(),
            json={"project_id": project_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == client_id  # implied by the project
    assert body["assignment_source"] == "manual"

    meeting = await repo.get_meeting(meeting_id)
    assert meeting.client_id == client_id
    assert meeting.project_id == project_id
    assert meeting.assignment_source == "manual"
    assert meeting.assignment_confidence == 1.0


@pytest.mark.asyncio
async def test_clearing_assignment_resets_source(api):
    repo = api["repo"]
    cp_repo = api["cp_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    client_id = await cp_repo.create_client(name="Acme")
    await repo.update_meeting(
        meeting_id, client_id=client_id, assignment_source="manual", assignment_confidence=1.0
    )

    with TestClient(api["app"]) as c:
        resp = c.patch(
            f"/api/meetings/{meeting_id}/assignment",
            headers=_auth_headers(),
            json={"client_id": None, "project_id": None},
        )

    assert resp.status_code == 200
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.client_id is None
    assert meeting.project_id is None
    assert meeting.assignment_source == ""


@pytest.mark.asyncio
async def test_assignment_404s(api):
    cp_repo = api["cp_repo"]
    repo = api["repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await cp_repo.create_client(name="Acme")

    with TestClient(api["app"]) as c:
        assert (
            c.patch(
                "/api/meetings/nope/assignment",
                headers=_auth_headers(),
                json={"client_id": None},
            ).status_code
            == 404
        )
        assert (
            c.patch(
                f"/api/meetings/{meeting_id}/assignment",
                headers=_auth_headers(),
                json={"client_id": "nope"},
            ).status_code
            == 404
        )
        assert (
            c.patch(
                f"/api/meetings/{meeting_id}/assignment",
                headers=_auth_headers(),
                json={"project_id": "nope"},
            ).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_assignment_rejects_project_of_different_client(api):
    repo = api["repo"]
    cp_repo = api["cp_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    client_a = await cp_repo.create_client(name="A")
    client_b = await cp_repo.create_client(name="B")
    project_b = await cp_repo.create_project(name="B proj", client_id=client_b)

    with TestClient(api["app"]) as c:
        resp = c.patch(
            f"/api/meetings/{meeting_id}/assignment",
            headers=_auth_headers(),
            json={"client_id": client_a, "project_id": project_b},
        )
    assert resp.status_code == 422
