from types import SimpleNamespace

from src.automations.evaluator import (
    build_meeting_context,
    domains_from_attendees,
    matches,
)

_CTX = {
    "tags": ["Type/Discovery", "Client/Acme"],
    "client_id": "c1",
    "project_id": "p1",
    "title": "Acme Discovery call",
    "attendee_domains": ["acme.com"],
}


def _rule(conditions, match_mode="all"):
    return {"match_mode": match_mode, "conditions": conditions}


def test_tag_condition():
    assert matches(_CTX, _rule([{"field": "tag", "value": "Type/Discovery"}])) is True
    assert matches(_CTX, _rule([{"field": "tag", "value": "Type/Review"}])) is False


def test_client_project_title_domain():
    assert matches(_CTX, _rule([{"field": "client", "value": "c1"}])) is True
    assert matches(_CTX, _rule([{"field": "project", "value": "pX"}])) is False
    assert matches(_CTX, _rule([{"field": "title_contains", "value": "discovery"}])) is True
    assert matches(_CTX, _rule([{"field": "attendee_domain", "value": "ACME.com"}])) is True


def test_all_vs_any():
    conds = [
        {"field": "tag", "value": "Type/Discovery"},
        {"field": "tag", "value": "Type/Review"},
    ]
    assert matches(_CTX, _rule(conds, "all")) is False
    assert matches(_CTX, _rule(conds, "any")) is True


def test_empty_conditions_and_unknown_field():
    assert matches(_CTX, _rule([])) is False
    assert matches(_CTX, _rule([{"field": "nonsense", "value": "x"}])) is False


def test_domains_from_attendees_handles_dicts_and_strings():
    j = '[{"name": "A", "email": "a@Acme.com"}, "b@beta.io", {"name": "no email"}]'
    assert domains_from_attendees(j) == ["acme.com", "beta.io"]
    assert domains_from_attendees("") == []
    assert domains_from_attendees("not json") == []


def test_build_meeting_context():
    meeting = SimpleNamespace(
        tags=["T"],
        client_id="c1",
        project_id=None,
        title="Hi",
        attendees_json='[{"email": "x@y.com"}]',
    )
    ctx = build_meeting_context(meeting)
    assert ctx["tags"] == ["T"]
    assert ctx["client_id"] == "c1"
    assert ctx["attendee_domains"] == ["y.com"]
