"""Tests for MeetingRepository.relink_orphaned_pending_audio.

The deferred-stop deadlock (fixed in the recording route) created
'pending' meeting rows whose audio_path was never written. The WAV
itself usually survives in the durable audio dir under a name derived
from the capture start time (meeting_%Y%m%d_%H%M%S.wav). This startup
repair relinks such rows to their file so "Process Now" works again,
and flips truly audio-less rows to 'error' instead of leaving them
'pending' forever.
"""

import time

import pytest

from src.db.repository import MeetingRepository


def _wav_name(started_at: float, suffix: str = "") -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(started_at))
    return f"meeting_{stamp}{suffix}.wav"


def _write_wav(audio_dir, started_at: float, suffix: str = ""):
    path = audio_dir / _wav_name(started_at, suffix)
    path.write_bytes(b"RIFF" + b"\x00" * 40)
    return path


@pytest.fixture
def audio_dir(tmp_path):
    d = tmp_path / "audio"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_relinks_pending_row_to_matching_file(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    wav = _write_wav(audio_dir, started_at)
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (1, 0)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.audio_path == str(wav)
    assert meeting.status == "pending"


@pytest.mark.asyncio
async def test_matches_file_stamped_a_few_seconds_earlier(repo: MeetingRepository, audio_dir):
    """The filename is stamped inside capture.start(); started_at is taken
    after start() returns, so the file can be a couple of seconds older."""
    started_at = time.time() - 3600
    wav = _write_wav(audio_dir, started_at - 3)
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (1, 0)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.audio_path == str(wav)


@pytest.mark.asyncio
async def test_file_outside_tolerance_is_not_matched(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    _write_wav(audio_dir, started_at - 60)
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (0, 1)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.audio_path is None
    assert meeting.status == "error"


@pytest.mark.asyncio
async def test_ignores_system_and_mic_source_wavs(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    _write_wav(audio_dir, started_at, suffix="_system")
    _write_wav(audio_dir, started_at, suffix="_mic")
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (0, 1)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.audio_path is None
    assert meeting.status == "error"


@pytest.mark.asyncio
async def test_does_not_steal_file_claimed_by_another_meeting(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    wav = _write_wav(audio_dir, started_at)

    owner_id = await repo.create_meeting(started_at=started_at, status="complete")
    await repo.update_meeting(owner_id, audio_path=str(wav))
    orphan_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (0, 1)
    orphan = await repo.get_meeting(orphan_id)
    assert orphan.audio_path is None
    assert orphan.status == "error"
    owner = await repo.get_meeting(owner_id)
    assert owner.audio_path == str(wav)
    assert owner.status == "complete"


@pytest.mark.asyncio
async def test_no_surviving_file_flips_row_to_error(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (0, 1)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.status == "error"


@pytest.mark.asyncio
async def test_healthy_rows_are_untouched(repo: MeetingRepository, audio_dir):
    started_at = time.time() - 3600
    wav = _write_wav(audio_dir, started_at)

    pending_with_path = await repo.create_meeting(started_at=started_at, status="pending")
    await repo.update_meeting(pending_with_path, audio_path=str(wav))
    complete_id = await repo.create_meeting(started_at=started_at - 100, status="complete")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (0, 0)
    assert (await repo.get_meeting(pending_with_path)).status == "pending"
    assert (await repo.get_meeting(complete_id)).status == "complete"


@pytest.mark.asyncio
async def test_two_orphans_each_get_their_own_file(repo: MeetingRepository, audio_dir):
    t1 = time.time() - 7200
    t2 = time.time() - 3600
    wav1 = _write_wav(audio_dir, t1)
    wav2 = _write_wav(audio_dir, t2)
    id1 = await repo.create_meeting(started_at=t1, status="pending")
    id2 = await repo.create_meeting(started_at=t2, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(audio_dir)

    assert (relinked, errored) == (2, 0)
    assert (await repo.get_meeting(id1)).audio_path == str(wav1)
    assert (await repo.get_meeting(id2)).audio_path == str(wav2)


@pytest.mark.asyncio
async def test_missing_audio_dir_is_harmless(repo: MeetingRepository, tmp_path):
    started_at = time.time() - 3600
    meeting_id = await repo.create_meeting(started_at=started_at, status="pending")

    relinked, errored = await repo.relink_orphaned_pending_audio(tmp_path / "does-not-exist")

    assert (relinked, errored) == (0, 1)
    assert (await repo.get_meeting(meeting_id)).status == "error"
