import pytest

from src.automations.repository import AutomationRepository


@pytest.fixture
async def auto_repo(db):
    return AutomationRepository(db)


@pytest.mark.asyncio
async def test_rule_crud(auto_repo):
    rid = await auto_repo.create(
        name="Tag discovery",
        match_mode="any",
        conditions=[{"field": "tag", "value": "Type/Discovery"}],
        actions=[{"type": "apply_tag", "tags": ["Reviewed"]}],
    )
    r = await auto_repo.get(rid)
    assert r["name"] == "Tag discovery"
    assert r["match_mode"] == "any"
    assert r["enabled"] is True
    assert r["conditions"] == [{"field": "tag", "value": "Type/Discovery"}]
    assert r["actions"] == [{"type": "apply_tag", "tags": ["Reviewed"]}]
    await auto_repo.update(rid, enabled=False, name="Tag discovery mtgs")
    r = await auto_repo.get(rid)
    assert r["enabled"] is False
    assert r["name"] == "Tag discovery mtgs"
    assert await auto_repo.list_rules(enabled_only=True) == []
    assert len(await auto_repo.list_rules()) == 1
    assert await auto_repo.delete(rid) is True
    assert await auto_repo.get(rid) is None


@pytest.mark.asyncio
async def test_dispatch_dedupe(auto_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    rid = await auto_repo.create(name="R", conditions=[{"field": "tag", "value": "x"}], actions=[])
    assert await auto_repo.has_dispatched(rid, mid) is False
    await auto_repo.record_dispatch(rid, mid)
    assert await auto_repo.has_dispatched(rid, mid) is True
    # Idempotent: a second record must not raise (INSERT OR IGNORE).
    await auto_repo.record_dispatch(rid, mid)
    fired = await auto_repo.fired_rules_for_meeting(mid)
    assert fired == [{"id": rid, "name": "R"}]


@pytest.mark.asyncio
async def test_fired_survives_rule_delete(auto_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    rid = await auto_repo.create(name="R", conditions=[{"field": "tag", "value": "x"}], actions=[])
    await auto_repo.record_dispatch(rid, mid)
    await auto_repo.delete(rid)
    # Dispatch row survives (no FK on rule_id); join drops the now-missing name.
    assert await auto_repo.fired_rules_for_meeting(mid) == []
