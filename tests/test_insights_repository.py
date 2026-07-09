"""Tests for src/insights/repository.py — definition CRUD + reprocess-safe results."""

import pytest

from src.insights.repository import InsightRepository


@pytest.fixture
async def insight_repo(db):
    return InsightRepository(db)


@pytest.mark.asyncio
async def test_definition_crud(insight_repo):
    did = await insight_repo.create(name="Risks", prompt="List risks raised.")
    d = await insight_repo.get(did)
    assert d["name"] == "Risks"
    assert d["prompt"] == "List risks raised."
    assert d["enabled"] is True

    await insight_repo.update(did, enabled=False, name="Risks & blockers")
    d = await insight_repo.get(did)
    assert d["enabled"] is False
    assert d["name"] == "Risks & blockers"

    assert await insight_repo.list_definitions(enabled_only=True) == []
    assert len(await insight_repo.list_definitions()) == 1

    assert await insight_repo.delete(did) is True
    assert await insight_repo.get(did) is None


@pytest.mark.asyncio
async def test_replace_results_is_reprocess_safe(insight_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    did = await insight_repo.create(name="Risks", prompt="p")
    first = [
        {"definition_id": did, "definition_name": "Risks", "content": "a", "speaker": ""},
        {"definition_id": did, "definition_name": "Risks", "content": "b", "speaker": "Me"},
    ]
    assert await insight_repo.replace_results_for_meeting(mid, first) == 2
    assert await insight_repo.replace_results_for_meeting(mid, first[:1]) == 1
    results = await insight_repo.results_for_meeting(mid)
    assert len(results) == 1
    assert results[0]["definition_name"] == "Risks"


@pytest.mark.asyncio
async def test_results_survive_definition_delete(insight_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    did = await insight_repo.create(name="Risks", prompt="p")
    await insight_repo.replace_results_for_meeting(
        mid,
        [{"definition_id": did, "definition_name": "Risks", "content": "a", "speaker": ""}],
    )
    await insight_repo.delete(did)
    results = await insight_repo.results_for_meeting(mid)
    assert len(results) == 1
    assert results[0]["definition_name"] == "Risks"
