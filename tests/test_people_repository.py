"""Tests for PersonRepository — people CRUD + voice-profile samples."""

import pytest

from src.people.repository import PersonRepository


@pytest.fixture
async def person_repo(db):
    return PersonRepository(db)


@pytest.mark.asyncio
async def test_create_and_get(person_repo):
    person_id = await person_repo.create(
        name="Sarah Chen", email="sarah@acme.com", aliases=["SC"], notes="Acme PM"
    )
    person = await person_repo.get(person_id)
    assert person["name"] == "Sarah Chen"
    assert person["email"] == "sarah@acme.com"
    assert person["aliases"] == ["SC"]
    assert person["notes"] == "Acme PM"
    assert person["is_me"] is False
    assert person["sample_count"] == 0


@pytest.mark.asyncio
async def test_list_people_sorted_case_insensitively(person_repo):
    await person_repo.create(name="zoe")
    await person_repo.create(name="Adam")
    people = await person_repo.list_people()
    assert [p["name"] for p in people] == ["Adam", "zoe"]


@pytest.mark.asyncio
async def test_update_fields(person_repo):
    person_id = await person_repo.create(name="Old Name")
    await person_repo.update(person_id, name="New Name", aliases_json=["NN"], is_me=True)
    person = await person_repo.get(person_id)
    assert person["name"] == "New Name"
    assert person["aliases"] == ["NN"]
    assert person["is_me"] is True


@pytest.mark.asyncio
async def test_update_rejects_unknown_columns(person_repo):
    person_id = await person_repo.create(name="X")
    with pytest.raises(ValueError):
        await person_repo.update(person_id, sample_count=99)


@pytest.mark.asyncio
async def test_delete_cascades_voice_samples(person_repo, db):
    person_id = await person_repo.create(name="Gone")
    await person_repo.add_voice_sample(person_id, [0.1, 0.2, 0.3])

    assert await person_repo.delete(person_id) is True
    cursor = await db.conn.execute(
        "SELECT COUNT(*) FROM voice_profiles WHERE person_id = ?", (person_id,)
    )
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_find_by_name_matches_name_and_alias(person_repo):
    person_id = await person_repo.create(name="Sarah Chen", aliases=["Saz", "S. Chen"])
    assert (await person_repo.find_by_name("sarah chen"))["id"] == person_id
    assert (await person_repo.find_by_name("saz"))["id"] == person_id
    assert await person_repo.find_by_name("nobody") is None


@pytest.mark.asyncio
async def test_voice_sample_round_trip(person_repo):
    person_id = await person_repo.create(name="Sarah")
    sample_id = await person_repo.add_voice_sample(
        person_id,
        [0.5, 0.25, -0.5],
        source_meeting_id="m1",
        speaker_label="Remote",
        segment_count=7,
        duration_seconds=42.5,
    )

    samples = await person_repo.list_voice_samples(person_id)
    assert len(samples) == 1
    assert samples[0]["id"] == sample_id
    assert samples[0]["segment_count"] == 7
    assert samples[0]["source_meeting_id"] == "m1"
    assert "embedding" not in samples[0]

    profiles = await person_repo.get_all_voice_profiles()
    assert len(profiles) == 1
    assert profiles[0]["person_id"] == person_id
    assert profiles[0]["name"] == "Sarah"
    assert profiles[0]["embedding"] == pytest.approx([0.5, 0.25, -0.5])

    person = await person_repo.get(person_id)
    assert person["sample_count"] == 1


@pytest.mark.asyncio
async def test_add_voice_sample_prunes_oldest_beyond_max(person_repo):
    person_id = await person_repo.create(name="Sarah")
    ids = []
    for i in range(4):
        ids.append(await person_repo.add_voice_sample(person_id, [float(i)], max_samples=3))

    samples = await person_repo.list_voice_samples(person_id)
    remaining = {s["id"] for s in samples}
    assert len(samples) == 3
    assert ids[0] not in remaining, "oldest sample must be pruned"


@pytest.mark.asyncio
async def test_delete_voice_sample(person_repo):
    person_id = await person_repo.create(name="Sarah")
    sample_id = await person_repo.add_voice_sample(person_id, [0.1])
    assert await person_repo.delete_voice_sample(sample_id) is True
    assert await person_repo.delete_voice_sample(sample_id) is False
    assert await person_repo.list_voice_samples(person_id) == []
