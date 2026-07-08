"""Tests for src/api/routes/people.py — people CRUD + speaker assignment."""

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import people as people_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.people.repository import PersonRepository

TEST_TOKEN = "test-token-for-people-tests"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    """Never read the developer's real config.yaml from route tests."""
    from src.utils.config import AppConfig

    monkeypatch.setattr(people_routes, "load_config", lambda *a, **k: AppConfig())


@pytest.fixture
async def api(tmp_path):
    """Real SQLite-backed app for the people routes."""
    db = Database(db_path=tmp_path / "people_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    person_repo = PersonRepository(db)
    people_routes.init(repo, person_repo)

    app = FastAPI()
    app.include_router(people_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "person_repo": person_repo}
    await db.close()


@pytest.mark.asyncio
async def test_people_crud_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/people",
            headers=_auth_headers(),
            json={"name": "Sarah Chen", "email": "sarah@acme.com", "aliases": ["SC"]},
        )
        assert created.status_code == 201
        person = created.json()
        assert person["name"] == "Sarah Chen"
        assert person["sample_count"] == 0
        person_id = person["id"]

        listed = c.get("/api/people", headers=_auth_headers()).json()
        assert [p["name"] for p in listed] == ["Sarah Chen"]

        patched = c.patch(
            f"/api/people/{person_id}",
            headers=_auth_headers(),
            json={"notes": "Acme project lead", "aliases": ["SC", "Saz"]},
        )
        assert patched.status_code == 200
        assert patched.json()["notes"] == "Acme project lead"
        assert patched.json()["aliases"] == ["SC", "Saz"]

        deleted = c.delete(f"/api/people/{person_id}", headers=_auth_headers())
        assert deleted.status_code == 200
        assert c.get("/api/people", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_patch_and_delete_missing_person_404(api):
    with TestClient(api["app"]) as c:
        assert (
            c.patch("/api/people/nope", headers=_auth_headers(), json={"name": "X"}).status_code
            == 404
        )
        assert c.delete("/api/people/nope", headers=_auth_headers()).status_code == 404
        assert c.get("/api/people/nope/voice-samples", headers=_auth_headers()).status_code == 404


@pytest.mark.asyncio
async def test_create_person_requires_name(api):
    with TestClient(api["app"]) as c:
        resp = c.post("/api/people", headers=_auth_headers(), json={"name": ""})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_assign_person_renames_speaker_and_links_person(api):
    repo = api["repo"]
    person_repo = api["person_repo"]

    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    transcript = {
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "hello", "speaker": "Remote"},
            {"start": 3.0, "end": 6.0, "text": "hi", "speaker": "Me"},
        ]
    }
    await repo.update_meeting(meeting_id, transcript_json=json.dumps(transcript))
    person_id = await person_repo.create(name="Sarah Chen")

    with TestClient(api["app"]) as c:
        resp = c.post(
            f"/api/meetings/{meeting_id}/speakers/Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": person_id, "enrol_voice": False},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Sarah Chen"
    assert body["enrolled"] is False

    meeting = await repo.get_meeting(meeting_id)
    segments = json.loads(meeting.transcript_json)["segments"]
    assert segments[0]["speaker"] == "Sarah Chen"
    assert segments[1]["speaker"] == "Me"

    mappings = await repo.get_speaker_names(meeting_id)
    assert mappings[0]["person_id"] == person_id
    assert mappings[0]["source"] == "manual"


@pytest.mark.asyncio
async def test_assign_person_enrolment_degrades_without_speechbrain(api, monkeypatch):
    """enrol_voice=True without speechbrain installed reports why."""
    import src.voice.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "is_voice_id_available", lambda: False)

    repo = api["repo"]
    person_repo = api["person_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(
        meeting_id,
        transcript_json=json.dumps(
            {"segments": [{"start": 0.0, "end": 3.0, "text": "x", "speaker": "Remote"}]}
        ),
    )
    person_id = await person_repo.create(name="Sarah")

    with TestClient(api["app"]) as c:
        resp = c.post(
            f"/api/meetings/{meeting_id}/speakers/Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": person_id, "enrol_voice": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled"] is False
    assert "not available" in body["reason"]


@pytest.mark.asyncio
async def test_assign_person_enrols_voice_sample_with_fake_embedder(api, monkeypatch, tmp_path):
    """Full enrolment path with a fake ECAPA embedder."""
    import numpy as np

    import src.voice.embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "is_voice_id_available", lambda: True)

    class FakeVoiceEmbedder:
        def __init__(self, *a, **k):
            pass

        def embed_windows(self, audio_path, windows):
            return [np.array([1.0, 0.0], dtype=np.float32) for _ in windows]

    monkeypatch.setattr(embedder_mod, "VoiceEmbedder", FakeVoiceEmbedder)

    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"\x00" * 128)

    repo = api["repo"]
    person_repo = api["person_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(
        meeting_id,
        audio_path=str(audio),
        transcript_json=json.dumps(
            {"segments": [{"start": 0.0, "end": 3.0, "text": "x", "speaker": "Remote"}]}
        ),
    )
    person_id = await person_repo.create(name="Sarah")

    with TestClient(api["app"]) as c:
        resp = c.post(
            f"/api/meetings/{meeting_id}/speakers/Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": person_id, "enrol_voice": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled"] is True
    assert body["sample_count"] == 1

    samples = await person_repo.list_voice_samples(person_id)
    assert samples[0]["source_meeting_id"] == meeting_id
    assert samples[0]["speaker_label"] == "Remote"


@pytest.mark.asyncio
async def test_assign_person_404s(api):
    repo = api["repo"]
    person_repo = api["person_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    person_id = await person_repo.create(name="Sarah")

    with TestClient(api["app"]) as c:
        missing_meeting = c.post(
            "/api/meetings/nope/speakers/Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": person_id},
        )
        missing_person = c.post(
            f"/api/meetings/{meeting_id}/speakers/Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": "nope"},
        )

    assert missing_meeting.status_code == 404
    assert missing_person.status_code == 404
