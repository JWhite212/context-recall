import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.automations.executor import ActionExecutor


def _ctx():
    return {
        "tags": ["Existing"],
        "client_id": None,
        "project_id": None,
        "title": "T",
        "attendee_domains": [],
    }


def test_apply_tag_dedupes_and_persists():
    repo = MagicMock()
    repo.update_meeting = AsyncMock()
    ex = ActionExecutor(repo, emit=lambda *a, **k: None)
    ctx = _ctx()
    rule = {"name": "R", "actions": [{"type": "apply_tag", "tags": ["Existing", "New"]}]}
    asyncio.run(ex.run_rule(rule, ctx, "m1", run_side_effects=True))
    repo.update_meeting.assert_awaited_once_with("m1", tags=["Existing", "New"])
    assert ctx["tags"] == ["Existing", "New"]


def test_webhook_runs_only_with_side_effects():
    repo = MagicMock()
    repo.update_meeting = AsyncMock()
    rule = {
        "name": "R",
        "actions": [{"type": "webhook", "url": "https://h/x", "format": "generic"}],
    }
    with patch("src.automations.executor.send_webhook", new=AsyncMock(return_value=True)) as sw:
        ex = ActionExecutor(repo, emit=lambda *a, **k: None)
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=False))
        sw.assert_not_awaited()
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=True))
        sw.assert_awaited_once()
        cfg = sw.await_args.args[0]
        assert cfg.url == "https://h/x"
        assert cfg.format == "generic"


def test_notify_emits_and_banners():
    repo = MagicMock()
    events = []
    rule = {"name": "R", "actions": [{"type": "notify", "message": "heads up"}]}
    with patch("src.automations.executor.macos_send", new=AsyncMock()) as banner:
        ex = ActionExecutor(repo, emit=lambda et, **k: events.append((et, k)))
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=True))
        banner.assert_awaited_once()
        assert events and events[0][0] == "notification"
