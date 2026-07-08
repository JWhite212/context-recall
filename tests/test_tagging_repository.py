"""Tests for ClientProjectRepository — clients/projects CRUD + helpers."""

import pytest

from src.tagging.repository import ClientProjectRepository


@pytest.fixture
async def cp_repo(db):
    return ClientProjectRepository(db)


@pytest.mark.asyncio
async def test_client_crud_round_trip(cp_repo):
    client_id = await cp_repo.create_client(
        name="Acme Corp",
        description="Widgets",
        aliases=["Acme"],
        email_domains=["@Acme.com", "acme.io"],
    )
    client = await cp_repo.get_client(client_id)
    assert client["name"] == "Acme Corp"
    assert client["aliases"] == ["Acme"]
    assert client["email_domains"] == ["acme.com", "acme.io"]  # normalised

    await cp_repo.update_client(client_id, description="Bigger widgets")
    assert (await cp_repo.get_client(client_id))["description"] == "Bigger widgets"

    assert [c["id"] for c in await cp_repo.list_clients()] == [client_id]


@pytest.mark.asyncio
async def test_archived_clients_hidden_by_default(cp_repo):
    client_id = await cp_repo.create_client(name="Old Client")
    await cp_repo.update_client(client_id, status="archived")
    assert await cp_repo.list_clients() == []
    assert len(await cp_repo.list_clients(include_archived=True)) == 1


@pytest.mark.asyncio
async def test_project_crud_and_client_filter(cp_repo):
    client_id = await cp_repo.create_client(name="Acme")
    p1 = await cp_repo.create_project(name="Portal", client_id=client_id)
    await cp_repo.create_project(name="Internal Tooling")

    acme_projects = await cp_repo.list_projects(client_id=client_id)
    assert [p["id"] for p in acme_projects] == [p1]
    assert len(await cp_repo.list_projects()) == 2


@pytest.mark.asyncio
async def test_delete_client_unassigns_meetings_and_unlinks_projects(cp_repo, repo, db):
    client_id = await cp_repo.create_client(name="Acme")
    project_id = await cp_repo.create_project(name="Portal", client_id=client_id)
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(
        meeting_id, client_id=client_id, project_id=project_id, assignment_source="manual"
    )

    assert await cp_repo.delete_client(client_id) is True

    meeting = await repo.get_meeting(meeting_id)
    assert meeting.client_id is None
    assert meeting.project_id == project_id  # project link survives
    project = await cp_repo.get_project(project_id)
    assert project["client_id"] is None  # FK ON DELETE SET NULL


@pytest.mark.asyncio
async def test_delete_project_unassigns_meetings(cp_repo, repo):
    project_id = await cp_repo.create_project(name="Portal")
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(meeting_id, project_id=project_id, assignment_source="auto")

    assert await cp_repo.delete_project(project_id) is True
    assert (await repo.get_meeting(meeting_id)).project_id is None


@pytest.mark.asyncio
async def test_latest_assignment_for_series_prefers_manual(cp_repo, repo):
    client_a = await cp_repo.create_client(name="A")
    client_b = await cp_repo.create_client(name="B")

    older = await repo.create_meeting(started_at=1000.0, status="complete")
    newer = await repo.create_meeting(started_at=2000.0, status="complete")
    await repo.update_meeting(older, series_id="s1", client_id=client_a, assignment_source="manual")
    await repo.update_meeting(newer, series_id="s1", client_id=client_b, assignment_source="auto")

    latest = await cp_repo.latest_assignment_for_series("s1")
    assert latest["client_id"] == client_a  # manual beats newer auto

    assert await cp_repo.latest_assignment_for_series("unknown") is None


@pytest.mark.asyncio
async def test_meeting_list_filters_by_assignment(cp_repo, repo):
    client_id = await cp_repo.create_client(name="Acme")
    m1 = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.create_meeting(started_at=2000.0, status="complete")
    await repo.update_meeting(m1, client_id=client_id, assignment_source="auto")

    filtered = await repo.list_meetings(client_id=client_id)
    assert [m.id for m in filtered] == [m1]
    assert (await repo.get_meeting(m1)).to_dict()["client_id"] == client_id


@pytest.mark.asyncio
async def test_count_meetings_matches_assignment_filters(cp_repo, repo):
    client_id = await cp_repo.create_client(name="Acme")
    m1 = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.create_meeting(started_at=2000.0, status="complete")
    await repo.update_meeting(m1, client_id=client_id, assignment_source="auto")

    assert await repo.count_meetings(client_id=client_id) == 1
    assert await repo.count_meetings() == 2
