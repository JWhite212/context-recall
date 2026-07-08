"""Tests for src/api/routes/ask.py — RAG answer with citations."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import ask as ask_routes
from src.utils.config import AppConfig

TEST_TOKEN = "test-token-for-ask-tests"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(ask_routes, "load_config", lambda *a, **k: AppConfig())


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _make_meeting(meeting_id, title):
    m = MagicMock()
    m.id = meeting_id
    m.title = title
    m.started_at = 1700000000.0
    m.summary_markdown = f"Summary of {title}"
    return m


def _make_app(repo, embedder=None) -> FastAPI:
    ask_routes.init(repo, embedder)
    app = FastAPI()
    app.include_router(ask_routes.router, dependencies=[Depends(verify_token)])
    return app


def test_ask_answers_with_citations_via_hybrid_search():
    repo = MagicMock()
    repo.search_hybrid = AsyncMock(
        return_value=[
            {
                "meeting_id": "m1",
                "segment_index": 2,
                "text": "We agreed the launch moves to March.",
                "speaker": "Sarah",
                "start_time": 120.0,
            }
        ]
    )
    repo.get_meetings_by_ids = AsyncMock(return_value=[_make_meeting("m1", "Launch sync")])

    embedder = MagicMock()
    embedder.embed_single.return_value = [0.1] * 4

    captured = {}

    def fake_chat(self, system, user):
        captured["system"] = system
        captured["user"] = user
        return "The launch moved to March [1]."

    app = _make_app(repo, embedder)
    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", fake_chat):
            resp = c.post(
                "/api/ask",
                headers=_auth_headers(),
                json={"question": "When is the launch?"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "The launch moved to March [1]."
    assert body["sources"][0]["meeting_id"] == "m1"
    assert body["sources"][0]["title"] == "Launch sync"
    assert "Launch sync" in captured["user"]
    assert "We agreed the launch moves to March." in captured["user"]
    assert "never invent" in captured["system"]


def test_ask_falls_back_to_fts_without_embedder():
    repo = MagicMock()
    repo.search_meetings = AsyncMock(return_value=[_make_meeting("m2", "Budget review")])
    repo.get_meetings_by_ids = AsyncMock(return_value=[_make_meeting("m2", "Budget review")])

    app = _make_app(repo, embedder=None)
    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", return_value="Answer [1]."):
            resp = c.post("/api/ask", headers=_auth_headers(), json={"question": "budget?"})

    assert resp.status_code == 200
    assert resp.json()["sources"][0]["meeting_id"] == "m2"
    repo.search_meetings.assert_awaited_once()


def test_ask_no_results():
    repo = MagicMock()
    repo.search_meetings = AsyncMock(return_value=[])

    app = _make_app(repo, embedder=None)
    with TestClient(app) as c:
        resp = c.post("/api/ask", headers=_auth_headers(), json={"question": "anything at all?"})

    assert resp.status_code == 200
    assert resp.json()["no_results"] is True


def test_ask_llm_failure_returns_502():
    repo = MagicMock()
    repo.search_meetings = AsyncMock(return_value=[_make_meeting("m1", "T")])
    repo.get_meetings_by_ids = AsyncMock(return_value=[_make_meeting("m1", "T")])

    app = _make_app(repo, embedder=None)
    with TestClient(app) as c:
        with patch("src.summariser.Summariser.chat", side_effect=RuntimeError("ollama down")):
            resp = c.post("/api/ask", headers=_auth_headers(), json={"question": "anything?"})

    assert resp.status_code == 502


def test_ask_validates_question_length():
    repo = MagicMock()
    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/ask", headers=_auth_headers(), json={"question": "hi"})
    assert resp.status_code == 422
