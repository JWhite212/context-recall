"""Tests for src/api/routes/meeting_insights.py — talk stats + email drafts."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import meeting_insights as insights_routes
from src.utils.config import AppConfig

TEST_TOKEN = "test-token-for-insights-tests"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(insights_routes, "load_config", lambda *a, **k: AppConfig())


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _make_meeting(**overrides):
    m = MagicMock()
    m.id = overrides.get("id", "m1")
    m.title = overrides.get("title", "Sprint planning")
    m.summary_markdown = overrides.get("summary_markdown", "## Summary\nWe planned.")
    m.attendees_json = overrides.get("attendees_json", '[{"name": "Sarah"}]')
    m.transcript_json = overrides.get(
        "transcript_json",
        json.dumps(
            {
                "segments": [
                    {"start": 0, "end": 30, "speaker": "Me", "text": "a"},
                    {"start": 30, "end": 40, "speaker": "Sarah", "text": "b"},
                ]
            }
        ),
    )
    return m


def _make_app(meeting, action_items=None) -> tuple[FastAPI, MagicMock]:
    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=meeting)
    ai_repo = MagicMock()
    ai_repo.list_by_meeting = AsyncMock(return_value=action_items or [])
    insights_routes.init(repo, ai_repo)
    app = FastAPI()
    app.include_router(insights_routes.router, dependencies=[Depends(verify_token)])
    return app, ai_repo


def test_talk_stats_endpoint():
    app, _ = _make_app(_make_meeting())
    with TestClient(app) as c:
        resp = c.get("/api/meetings/m1/talk-stats", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_speaking_seconds"] == 40.0
    assert body["speakers"][0]["speaker"] == "Me"


def test_talk_stats_404():
    app, _ = _make_app(None)
    with TestClient(app) as c:
        assert c.get("/api/meetings/x/talk-stats", headers=_auth_headers()).status_code == 404


def test_draft_email_composes_from_summary_and_action_items():
    items = [
        {"title": "Send deck", "assignee": "Sarah", "due_date": "2026-07-10", "status": "open"},
        {"title": "Cancelled thing", "status": "cancelled"},
    ]
    app, ai_repo = _make_app(_make_meeting(), action_items=items)

    captured = {}

    def fake_chat(self, system, user):
        captured["user"] = user
        return json.dumps(
            {"subject": "Follow-up: Sprint planning", "body": "Hi all,\n- Send deck (Sarah)"}
        )

    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", fake_chat):
            resp = c.post("/api/meetings/m1/draft-email", headers=_auth_headers(), json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "Follow-up: Sprint planning"
    assert "Send deck" in body["body"]
    assert "Send deck" in captured["user"]
    assert "Cancelled thing" not in captured["user"]
    ai_repo.list_by_meeting.assert_awaited_once_with("m1")


def test_draft_email_degrades_when_llm_ignores_json():
    app, _ = _make_app(_make_meeting())
    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", return_value="Hi team, quick recap..."):
            resp = c.post("/api/meetings/m1/draft-email", headers=_auth_headers(), json={})
    body = resp.json()
    assert body["subject"] == "Follow-up: Sprint planning"
    assert body["body"] == "Hi team, quick recap..."


def test_draft_email_400_without_summary():
    app, _ = _make_app(_make_meeting(summary_markdown=None))
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/draft-email", headers=_auth_headers(), json={})
    assert resp.status_code == 400


def test_draft_email_502_on_llm_failure():
    app, _ = _make_app(_make_meeting())
    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", side_effect=RuntimeError("down")):
            resp = c.post("/api/meetings/m1/draft-email", headers=_auth_headers(), json={})
    assert resp.status_code == 502
