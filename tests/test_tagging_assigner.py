"""Tests for src/tagging/assigner.py — deterministic + LLM assignment."""

import json
from unittest.mock import patch

import pytest

from src.tagging.assigner import (
    Assignment,
    LlmAssigner,
    build_context_text,
    deterministic_assignment,
)
from src.utils.config import SummarisationConfig, TaggingConfig

ACME = {
    "id": "c-acme",
    "name": "Acme Corp",
    "description": "Industrial widgets client. Key contact Sarah Chen.",
    "aliases": ["Acme"],
    "email_domains": ["acme.com"],
}
GLOBEX = {
    "id": "c-globex",
    "name": "Globex",
    "description": "",
    "aliases": [],
    "email_domains": ["globex.io"],
}
PORTAL = {
    "id": "p-portal",
    "client_id": "c-acme",
    "name": "Customer Portal",
    "description": "Rebuild of the Acme customer portal.",
    "aliases": ["portal rebuild"],
}
INTERNAL = {
    "id": "p-int",
    "client_id": None,
    "name": "Ops Automation",
    "description": "",
    "aliases": [],
}

ROSTER = {"clients": [ACME, GLOBEX], "projects": [PORTAL, INTERNAL]}


# ----------------------------------------------------------------------
# Deterministic pass
# ----------------------------------------------------------------------


def test_series_assignment_wins_over_everything():
    assignment = deterministic_assignment(
        ROSTER,
        attendees=[{"name": "X", "email": "x@globex.io"}],
        calendar_title="Globex sync",
        series_assignment={"client_id": "c-acme", "project_id": "p-portal"},
    )
    assert assignment.method == "series"
    assert assignment.client_id == "c-acme"
    assert assignment.project_id == "p-portal"


def test_email_domain_matches_client():
    assignment = deterministic_assignment(
        ROSTER, attendees=[{"name": "Sarah", "email": "sarah@acme.com"}]
    )
    assert assignment.method == "domain"
    assert assignment.client_id == "c-acme"
    assert assignment.project_id is None


def test_email_domain_plus_title_refines_to_project():
    assignment = deterministic_assignment(
        ROSTER,
        attendees=[{"name": "Sarah", "email": "sarah@acme.com"}],
        calendar_title="Customer Portal weekly",
    )
    assert assignment.client_id == "c-acme"
    assert assignment.project_id == "p-portal"


def test_generic_email_domains_ignored():
    assignment = deterministic_assignment(ROSTER, attendees=[{"name": "X", "email": "x@gmail.com"}])
    assert assignment is None


def test_project_alias_in_calendar_title():
    assignment = deterministic_assignment(ROSTER, calendar_title="Portal rebuild kickoff")
    assert assignment.method == "alias"
    assert assignment.project_id == "p-portal"
    assert assignment.client_id == "c-acme"  # implied by the project


def test_client_name_in_calendar_title():
    assignment = deterministic_assignment(ROSTER, calendar_title="Globex quarterly review")
    assert assignment.client_id == "c-globex"
    assert assignment.project_id is None


def test_no_signals_returns_none():
    assert deterministic_assignment(ROSTER, calendar_title="1:1 with Alex") is None


def test_short_names_never_match_as_substrings():
    roster = {
        "clients": [{"id": "c1", "name": "AB", "aliases": [], "email_domains": []}],
        "projects": [],
    }
    assert deterministic_assignment(roster, calendar_title="ABsolutely unrelated") is None


# ----------------------------------------------------------------------
# Context text
# ----------------------------------------------------------------------


def test_build_context_text_includes_descriptions():
    assignment = Assignment(client_id="c-acme", project_id="p-portal", confidence=0.9)
    text = build_context_text(ROSTER, assignment)
    assert "Acme Corp" in text
    assert "Industrial widgets" in text
    assert "Customer Portal" in text
    assert "Rebuild of the Acme customer portal." in text


def test_build_context_text_caps_length():
    roster = {
        "clients": [{**ACME, "description": "x" * 5000}],
        "projects": [],
    }
    text = build_context_text(roster, Assignment(client_id="c-acme"), max_chars=100)
    assert len(text) == 100


def test_build_context_text_none_when_nothing_matched():
    assert build_context_text(ROSTER, Assignment()) is None


# ----------------------------------------------------------------------
# LLM assigner
# ----------------------------------------------------------------------


def _assigner(min_confidence=0.6):
    return LlmAssigner(SummarisationConfig(), TaggingConfig(min_confidence=min_confidence))


def test_llm_assignment_parsed_and_client_implied_by_project():
    response = json.dumps(
        {"client_id": None, "project_id": "p-portal", "confidence": 0.82, "rationale": "x"}
    )
    assigner = _assigner()
    with patch.object(assigner, "_call_llm", return_value=response):
        assignment = assigner.assign(
            ROSTER, title="Weekly", summary_markdown="portal work", attendees=[]
        )
    assert assignment.project_id == "p-portal"
    assert assignment.client_id == "c-acme"
    assert assignment.method == "llm"
    assert assignment.confidence == pytest.approx(0.82)


def test_llm_assignment_rejects_low_confidence():
    response = json.dumps({"client_id": "c-acme", "project_id": None, "confidence": 0.3})
    assigner = _assigner()
    with patch.object(assigner, "_call_llm", return_value=response):
        assert assigner.assign(ROSTER, title="", summary_markdown="", attendees=[]) is None


def test_llm_assignment_rejects_invented_ids():
    response = json.dumps({"client_id": "c-fake", "project_id": "p-fake", "confidence": 0.99})
    assigner = _assigner()
    with patch.object(assigner, "_call_llm", return_value=response):
        assert assigner.assign(ROSTER, title="", summary_markdown="", attendees=[]) is None


def test_llm_assignment_handles_fenced_and_garbage():
    assigner = _assigner()
    fenced = '```json\n{"client_id": "c-acme", "project_id": null, "confidence": 0.9}\n```'
    with patch.object(assigner, "_call_llm", return_value=fenced):
        assignment = assigner.assign(ROSTER, title="", summary_markdown="", attendees=[])
    assert assignment.client_id == "c-acme"

    with patch.object(assigner, "_call_llm", return_value="no idea"):
        assert assigner.assign(ROSTER, title="", summary_markdown="", attendees=[]) is None


def test_llm_assignment_survives_backend_failure():
    assigner = _assigner()
    with patch.object(assigner, "_call_llm", side_effect=RuntimeError("down")):
        assert assigner.assign(ROSTER, title="", summary_markdown="", attendees=[]) is None


def test_llm_assignment_skipped_with_empty_roster():
    assigner = _assigner()
    with patch.object(assigner, "_call_llm") as call:
        result = assigner.assign(
            {"clients": [], "projects": []}, title="", summary_markdown="", attendees=[]
        )
    assert result is None
    call.assert_not_called()


def test_llm_pick_of_wrong_client_reconciled_to_projects_client():
    """A project's own client wins over a contradictory client pick."""
    response = json.dumps(
        {"client_id": "c-globex", "project_id": "p-portal", "confidence": 0.9}
    )
    assigner = _assigner()
    with patch.object(assigner, "_call_llm", return_value=response):
        assignment = assigner.assign(ROSTER, title="", summary_markdown="", attendees=[])
    assert assignment.project_id == "p-portal"
    assert assignment.client_id == "c-acme"
