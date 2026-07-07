"""Tests for src/audio_cleanup.py — temp-audio debris removal.

Production 2026-07-07: ~/Library/Caches/Context Recall held every
recording since May — silent WAVs, orphaned _system/_mic source files,
and 44-byte header-only stubs from failed stream starts. Nothing ever
cleaned the directory.
"""

import time
from pathlib import Path

from src.audio_cleanup import WAV_HEADER_BYTES, cleanup_temp_audio


def _make_wav(path: Path, *, size: int, age_days: float = 0.0) -> Path:
    path.write_bytes(b"\x00" * size)
    if age_days:
        stamp = time.time() - age_days * 86400
        import os

        os.utime(path, (stamp, stamp))
    return path


class TestCleanupTempAudio:
    def test_empty_stubs_removed_regardless_of_age(self, tmp_path):
        """44-byte files are WAV headers with zero frames — left behind
        whenever stream.start() failed (three on disk from the -9986
        era). They are garbage at any age."""
        stub = _make_wav(tmp_path / "meeting_20260707_182153_system.wav", size=WAV_HEADER_BYTES)
        removed = cleanup_temp_audio(tmp_path)
        assert stub in removed
        assert not stub.exists()

    def test_fresh_real_recordings_are_kept(self, tmp_path):
        wav = _make_wav(tmp_path / "meeting_20260707_120000.wav", size=200_000, age_days=1)
        removed = cleanup_temp_audio(tmp_path, max_age_days=14)
        assert wav.exists()
        assert removed == []

    def test_stale_recordings_are_removed(self, tmp_path):
        old = _make_wav(tmp_path / "meeting_20260507_095824.wav", size=400_000, age_days=60)
        old_src = _make_wav(tmp_path / "meeting_20260515_100414_mic.wav", size=180_000, age_days=53)
        removed = cleanup_temp_audio(tmp_path, max_age_days=14)
        assert not old.exists()
        assert not old_src.exists()
        assert set(removed) == {old, old_src}

    def test_active_recording_is_never_touched(self, tmp_path):
        active = _make_wav(
            tmp_path / "meeting_20260101_000000_system.wav", size=WAV_HEADER_BYTES, age_days=30
        )
        removed = cleanup_temp_audio(tmp_path, active_paths={active})
        assert active.exists()
        assert removed == []

    def test_non_meeting_files_are_untouched(self, tmp_path):
        stranger = _make_wav(tmp_path / "not_ours.wav", size=10, age_days=400)
        note = tmp_path / "meeting_notes.txt"
        note.write_text("keep me")
        cleanup_temp_audio(tmp_path)
        assert stranger.exists()
        assert note.exists()

    def test_missing_directory_is_a_noop(self, tmp_path):
        assert cleanup_temp_audio(tmp_path / "does-not-exist") == []

    def test_removal_errors_are_swallowed(self, tmp_path, monkeypatch):
        _make_wav(tmp_path / "meeting_20260101_000000.wav", size=10, age_days=30)

        def _boom(self):
            raise OSError("busy")

        monkeypatch.setattr(Path, "unlink", _boom)
        removed = cleanup_temp_audio(tmp_path, max_age_days=14)
        assert removed == []
