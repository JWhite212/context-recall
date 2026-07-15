"""Guard tests: the PyInstaller spec must bundle the frameworks and declare
the TCC usage keys that deployed features depend on. These features have
silently shipped broken before (voice-ID/speechbrain, sqlite-vec, and the
calendar reader) because a required hidden import or plist key was missing
from the spec. Asserting on the spec text catches that in CI without a build."""

from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "context-recall.spec"


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


@pytest.mark.parametrize("module", ['"EventKit"', '"Foundation"', '"CoreFoundation"', '"objc"'])
def test_spec_bundles_eventkit_modules(spec_text, module):
    assert module in spec_text, f"{module} missing from context-recall.spec hiddenimports"


def test_spec_collects_eventkit_submodules(spec_text):
    assert 'collect_submodules("EventKit")' in spec_text


@pytest.mark.parametrize(
    "key",
    [
        "NSCalendarsUsageDescription",
        "NSCalendarsFullAccessUsageDescription",
        "NSMicrophoneUsageDescription",  # regression: must not be dropped
    ],
)
def test_spec_declares_tcc_usage_keys(spec_text, key):
    assert key in spec_text, f"{key} missing from context-recall.spec info_plist"


@pytest.mark.parametrize(
    "module",
    ['"pyannote.audio"', '"pyannote.core"', '"pyannote.pipeline"'],
)
def test_spec_bundles_pyannote(spec_text, module):
    assert module in spec_text, f"{module} missing from context-recall.spec hiddenimports"


def test_spec_collects_pyannote_submodules(spec_text):
    assert 'collect_submodules("pyannote.audio")' in spec_text
