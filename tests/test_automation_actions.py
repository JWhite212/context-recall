import json
from types import SimpleNamespace

from src.automations.executor import ActionExecutor
from src.utils.config import SummarisationConfig


class _InsightRepo:
    def __init__(self, definition):
        self._def = definition
        self.written = None

    async def get(self, _id):
        return self._def

    async def replace_results_for_definition(self, meeting_id, definition_id, results):
        self.written = (meeting_id, definition_id, results)
        return len(results)

    async def results_for_meeting(self, _mid):
        return []


def _meeting(tj=None):
    return SimpleNamespace(
        id="m1",
        title="Armacell UAT",
        started_at=1.0,
        ended_at=61.0,
        tags=[],
        attendees_json="[]",
        summary_markdown="notes",
        transcript_json=tj
        or json.dumps(
            {"segments": [{"start": 0.0, "end": 5.0, "text": "hello world " * 20, "speaker": "S"}]}
        ),
    )


async def test_run_insight_writes_scoped_results(monkeypatch):
    definition = {
        "id": "d1",
        "name": "Client Call",
        "output_mode": "list",
        "fields": None,
        "prompt": "list things",
    }
    irepo = _InsightRepo(definition)
    # Stub the extractor so no LLM is called.
    from src.insights import extractor as ext_mod

    monkeypatch.setattr(
        ext_mod.InsightExtractor,
        "extract",
        lambda self, t, defs: [
            {
                "definition_id": "d1",
                "definition_name": "Client Call",
                "content": "x",
                "speaker": "",
                "fields": None,
            }
        ],
    )
    services = {
        "meeting": _meeting(),
        "insight_repo": irepo,
        "action_items_repo": None,
        "summarisation_config": SummarisationConfig(backend="ollama"),
    }
    ex = ActionExecutor(repo=None, emit=lambda *a, **k: None, services=services)
    rule = {"name": "R", "actions": [{"type": "run_insight", "definition_id": "d1"}]}
    await ex.run_rule(rule, context={"tags": []}, meeting_id="m1", run_side_effects=False)
    assert irepo.written[0] == "m1"
    assert irepo.written[1] == "d1"


async def test_send_notes_posts_signed_payload(monkeypatch):
    posted = {}

    async def fake_post(url, json_body, headers):
        posted["url"] = url
        posted["body"] = json_body
        posted["headers"] = headers
        return True

    ex = ActionExecutor(
        repo=None,
        emit=lambda *a, **k: None,
        services={
            "meeting": _meeting(),
            "insight_repo": _InsightRepo({}),
            "action_items_repo": SimpleNamespace(list_by_meeting=_aempty),
            "summarisation_config": None,
        },
    )
    monkeypatch.setattr(ex, "_post_json", fake_post)
    rule = {
        "name": "R",
        "actions": [
            {
                "type": "send_notes",
                "url": "https://x.test/hook",
                "secret": "whsec_1",
                "include_transcript": False,
            }
        ],
    }
    await ex.run_rule(rule, context={}, meeting_id="m1", run_side_effects=True)
    assert posted["url"] == "https://x.test/hook"
    assert posted["body"]["name"] == "Armacell UAT"
    assert "x-signature" in posted["headers"]


async def test_send_notes_skipped_when_not_side_effects(monkeypatch):
    called = []
    ex = ActionExecutor(
        repo=None,
        emit=lambda *a, **k: None,
        services={
            "meeting": _meeting(),
            "insight_repo": _InsightRepo({}),
            "action_items_repo": SimpleNamespace(list_by_meeting=_aempty),
            "summarisation_config": None,
        },
    )
    monkeypatch.setattr(ex, "_post_json", lambda *a, **k: called.append(1))
    rule = {"name": "R", "actions": [{"type": "send_notes", "url": "https://x", "secret": ""}]}
    await ex.run_rule(rule, context={}, meeting_id="m1", run_side_effects=False)
    assert called == []


async def _aempty(_mid):
    return []
