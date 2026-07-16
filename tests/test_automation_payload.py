import hashlib
import hmac
import json
from types import SimpleNamespace

from src.automations.payload import build_circleback_payload, sign_payload
from src.transcriber import Transcript


def _meeting(**kw):
    base = dict(
        id="m1",
        title="Armacell UAT",
        started_at=1_700_000_000.0,
        ended_at=1_700_000_600.0,
        tags=["ClientX"],
        attendees_json='[{"name":"Jamie","email":"j@x.com"}]',
        summary_markdown="- did things",
        transcript_json=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_payload_has_circleback_field_names():
    p = build_circleback_payload(_meeting(), action_items=[], insights=[])
    assert p["id"] == "m1"
    assert p["name"] == "Armacell UAT"
    assert p["notes"] == "- did things"
    assert p["tags"] == ["ClientX"]
    assert p["attendees"] == [{"name": "Jamie", "email": "j@x.com"}]
    assert p["duration"] == 600.0
    assert "createdAt" in p and p["createdAt"].startswith("20")
    assert "transcript" not in p  # include_transcript defaults False


def test_action_item_status_mapped():
    items = [
        {"id": "a", "title": "T", "description": "D", "assignee": "Jamie", "status": "completed"},
        {"id": "b", "title": "U", "description": "", "assignee": "unassigned", "status": "open"},
        {"id": "c", "title": "V", "description": "", "assignee": None, "status": "cancelled"},
    ]
    p = build_circleback_payload(_meeting(), action_items=items, insights=[])
    statuses = [ai["status"] for ai in p["actionItems"]]
    assert statuses == ["DONE", "PENDING"]  # cancelled omitted
    assert p["actionItems"][0]["assignee"] == {"name": "Jamie", "email": None}
    assert p["actionItems"][1]["assignee"] is None  # 'unassigned' -> null


def test_insights_grouped_list_and_structured():
    insights = [
        {
            "definition_name": "Questions",
            "content": "Is it live?",
            "speaker": "Sam",
            "fields": None,
        },
        {
            "definition_name": "Client Call",
            "content": "Go-live: 2026-09-02",
            "speaker": "",
            "fields": {"go_live": "2026-09-02"},
        },
    ]
    p = build_circleback_payload(_meeting(), action_items=[], insights=insights)
    assert p["insights"]["Questions"] == [{"insight": "Is it live?", "speaker": "Sam"}]
    assert p["insights"]["Client Call"] == [{"insight": {"go_live": "2026-09-02"}, "speaker": None}]


def test_include_transcript_when_requested():
    tj = json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "hi", "speaker": "Sam"}]})
    p = build_circleback_payload(
        _meeting(transcript_json=tj), action_items=[], insights=[], include_transcript=True
    )
    assert p["transcript"] == [{"speaker": "Sam", "text": "hi", "timestamp": 1.0}]


def test_sign_payload_matches_hmac_sha256():
    body = b'{"a":1}'
    sig = sign_payload(body, "whsec_test")
    assert sig == hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()


def test_transcript_from_dict_roundtrip():
    t = Transcript.from_dict(
        {
            "segments": [{"start": 1.0, "end": 2.0, "text": "hi", "speaker": "S"}],
            "language": "en",
            "duration_seconds": 2.0,
        }
    )
    assert t.full_text == "hi"
    assert t.segments[0].speaker == "S"
