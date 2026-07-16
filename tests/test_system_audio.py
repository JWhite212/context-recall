"""Tests for the system-audio backend abstraction."""

import stat
import sys
from pathlib import Path
from unittest.mock import patch

import src.system_audio as sa


def _make_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_resolve_helper_path_frozen(tmp_path):
    macos = tmp_path / "App.app" / "Contents" / "MacOS"
    resources = tmp_path / "App.app" / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    exe = macos / "context-recall-daemon"
    _make_exec(exe)
    helper = resources / sa.HELPER_NAME
    _make_exec(helper)
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
        assert sa.resolve_helper_path() == helper


def test_resolve_helper_path_frozen_missing(tmp_path):
    macos = tmp_path / "App.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "context-recall-daemon"
    _make_exec(exe)
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
        assert sa.resolve_helper_path() is None


def test_resolve_helper_path_dev(tmp_path):
    # __file__ lives at <root>/src/system_audio.py; the dev helper is at
    # <root>/macos/sck-audio-capture/.build/<HELPER_NAME>.
    fake_src = tmp_path / "src" / "system_audio.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text("")
    helper = tmp_path / "macos" / "sck-audio-capture" / ".build" / sa.HELPER_NAME
    helper.parent.mkdir(parents=True)
    _make_exec(helper)
    with patch.object(sa, "__file__", str(fake_src)):
        # not frozen
        with patch.object(sys, "frozen", False, create=True):
            assert sa.resolve_helper_path() == helper
