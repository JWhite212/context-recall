# Multi-Speaker Diarisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate multiple remote participants (`SPEAKER_00…N`) instead of collapsing everyone-but-me into a single "Remote", name recurring speakers via voice-ID, and give the user an Alter-style speaker-correction panel.

**Architecture:** The mic channel is unambiguously the local user, so diarisation stays a **hybrid**: the energy diariser decides _me vs. remote_ per segment (mic-vs-system RMS), and pyannote — run over the **remote/system** source WAV — separates the remote segments into `SPEAKER_00…N`. `pyannote` becomes the default backend but **degrades to energy** whenever torch/pyannote or the (gated) model is unavailable, so the pipeline never hard-fails. The existing voice-ID and attendee-seeding stages already resolve `SPEAKER_NN` labels; the correction panel reuses the existing speaker-rename endpoint and audio-seek handle.

**Tech Stack:** Python 3.12, `pyannote.audio` (optional/deferred), `torch`/`torchaudio` (already bundled), `soundfile`, `numpy`, PyInstaller; React 19 + TypeScript + TanStack Query + Vitest.

## Global Constraints

- **Hybrid, not pure pyannote.** The mic channel = `speaker_name` ("Me"); pyannote runs on the **remote/system** WAV only and separates the _remote_ segments. A segment already classified as the local user must stay "Me".
- **Backend values are `energy` | `pyannote`** (config `diarisation.backend`). Copy verbatim.
- **Degrade, never crash.** pyannote import failure, model-load failure (the model `pyannote/speaker-diarization-3.1` is **gated** and needs `HF_TOKEN`), or a missing source WAV → fall back to the energy backend (binary Me/Remote). Log the reason; never raise out of the pipeline.
- **No schema migration.** Reuse the existing `speaker_mappings` table and `repo.set_speaker_name(...)` / `repo.get_speaker_names(...)`. Manual speaker renames already survive reprocess via `PipelineRunner._reapply_speaker_mappings`.
- **Voice-ID and attendee-seeding are existing, multi-speaker-aware stages** — reuse them; do not reimplement. `VoiceRecogniser` already matches `^SPEAKER_\d+$` labels.
- **macOS + Apple-Silicon only.** Python: `python3 -m pytest tests/`, `ruff check src/ tests/`. UI: `cd ui && npm test`, `npx tsc --noEmit`. Conventional-commit messages.
- **Tests never load a real ML model.** pyannote and its pipeline are mocked; source WAVs are tiny generated fixtures.

---

## File Structure

- **Modify** `src/utils/config.py` — `DiarisationConfig.enabled` → `True`, `DiarisationConfig.backend` → `"pyannote"`.
- **Modify** `src/pyannote_diariser.py` — turn `PyAnnoteDiariser` into the hybrid: accept `mic_audio_path` + `system_audio_path`, reuse `EnergyDiariser` for me/remote, overlay `SPEAKER_NN` on remote segments; refactor pyannote turn-extraction into a `_speaker_turns()` helper.
- **Modify** `src/diariser.py` — `create_diariser` degrades to `EnergyDiariser` (logs) instead of raising when pyannote is unavailable.
- **Modify** `src/pipeline_runner.py` — `_diarise` derives the system source WAV and passes `mic_audio_path` + `system_audio_path` to the pyannote backend; wraps the diarise call so a runtime pyannote/model failure degrades to energy for that run.
- **Modify** `context-recall.spec` — collect `pyannote.audio` (+ `asteroid_filterbanks`, `pytorch_metric_learning`, `speechbrain` already present); extend the guard test.
- **Modify** `tests/test_spec_bundle_guards.py` — assert pyannote hidden imports.
- **Create** `ui/src/components/meetings/SpeakerPanel.tsx` — the Alter-style panel (list detected speakers, per-speaker segment count, play-their-segments, rename, assign-to-person).
- **Modify** `ui/src/components/meetings/MeetingDetail.tsx` — render `SpeakerPanel` above the transcript, wired to the audio-seek handle.
- **Modify** `ui/src/lib/api.ts` — none expected (reuse `setSpeakerName` / `assignPersonToSpeaker`); confirm they exist.

---

### Task 1: Default to the pyannote hybrid backend

**Files:**

- Modify: `src/utils/config.py` (`DiarisationConfig`, ~line 197-205)
- Test: `tests/test_config.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `DiarisationConfig().enabled is True`, `DiarisationConfig().backend == "pyannote"`. (Enabling diarisation also makes the orchestrator keep source WAVs — `src/main.py:109` sets `keep_source_files = True` when `diarisation.enabled`.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_diarisation_defaults_to_pyannote_enabled():
    from src.utils.config import DiarisationConfig

    cfg = DiarisationConfig()
    assert cfg.enabled is True
    assert cfg.backend == "pyannote"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_diarisation_defaults_to_pyannote_enabled -v`
Expected: FAIL — defaults are `False` / `"energy"`.

- [ ] **Step 3: Change the defaults**

In `src/utils/config.py`, in `DiarisationConfig`:

```python
class DiarisationConfig:
    enabled: bool = True
    speaker_name: str = "Me"  # Label for the local user.
    remote_label: str = "Remote"  # Label for remote participants.
    energy_ratio_threshold: float = 1.5  # How much louder one source must be.
    backend: str = "pyannote"  # "energy" or "pyannote"
    pyannote_model: str = "pyannote/speaker-diarization-3.1"
    num_speakers: int = 0  # 0 = auto-detect
```

- [ ] **Step 4: Run the test + ripple check**

Run: `python3 -m pytest tests/test_config.py tests/test_diariser.py tests/test_pipeline_runner.py -v`
Expected: PASS. If a diariser/pipeline test asserted the default backend was `energy` or that diarisation was off, update it to construct an explicit `DiarisationConfig(backend="energy")` for the energy-specific cases.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py tests/test_config.py
git commit -m "feat(diarisation): default to the pyannote hybrid backend"
```

---

### Task 2: Hybrid pyannote diariser (remote channel + energy me/remote)

**Files:**

- Modify: `src/pyannote_diariser.py`
- Test: `tests/test_pyannote_diariser.py` (create if absent; else extend)

**Interfaces:**

- Consumes: `EnergyDiariser` (from `src.diariser`), `DiarisationConfig`, `Transcript`.
- Produces: `PyAnnoteDiariser.diarise(self, transcript, audio_path, *, mic_audio_path: Path | None = None, system_audio_path: Path | None = None) -> Transcript` and `PyAnnoteDiariser._speaker_turns(self, audio_path: Path) -> list[tuple[float, float, str]]`. Behaviour: segments the mic-dominant windows keep `config.speaker_name`; every other segment gets the max-overlap `SPEAKER_NN` from pyannote (run on `system_audio_path` when it exists, else `audio_path`), falling back to `config.remote_label` when there is no overlap.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pyannote_diariser.py`:

```python
"""Hybrid pyannote diariser: energy me/remote + pyannote remote separation.
The pyannote pipeline is always mocked — no real model is loaded."""

from pathlib import Path

import numpy as np
import soundfile as sf

from src.pyannote_diariser import PyAnnoteDiariser
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import DiarisationConfig


def _wav(path: Path, loud: bool, seconds: float = 3.0, sr: int = 16000):
    n = int(seconds * sr)
    amp = 0.5 if loud else 0.0001
    sf.write(str(path), (np.random.randn(n) * amp).astype("float32"), sr)


def _transcript():
    return Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="hi", speaker=""),
            TranscriptSegment(start=1.0, end=2.0, text="hello there", speaker=""),
            TranscriptSegment(start=2.0, end=3.0, text="agreed", speaker=""),
        ],
        language="en",
    )


def _diariser_with_turns(turns):
    cfg = DiarisationConfig(backend="pyannote")
    d = PyAnnoteDiariser(cfg)
    # Stub the pyannote turn extraction — never load a real model.
    d._speaker_turns = lambda audio_path: turns  # type: ignore
    return d, cfg


def test_mic_dominant_segment_is_me(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=False)  # remote quiet
    _wav(mic, loud=True)  # user talking
    d, cfg = _diariser_with_turns([(0.0, 3.0, "SPEAKER_00")])
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert all(seg.speaker == cfg.speaker_name for seg in t.segments)


def test_remote_segments_get_pyannote_speakers(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=True)  # remote talking
    _wav(mic, loud=False)  # user quiet
    d, cfg = _diariser_with_turns(
        [(0.0, 1.5, "SPEAKER_00"), (1.5, 3.0, "SPEAKER_01")]
    )
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert t.segments[0].speaker == "SPEAKER_00"
    assert t.segments[2].speaker == "SPEAKER_01"
    assert cfg.speaker_name not in {s.speaker for s in t.segments}


def test_remote_segment_without_overlap_falls_back_to_remote_label(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=True)
    _wav(mic, loud=False)
    d, cfg = _diariser_with_turns([])  # pyannote found no turns
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert all(seg.speaker == cfg.remote_label for seg in t.segments)
```

(If `Transcript`/`TranscriptSegment` constructors differ, read `src/transcriber.py` and match them — some codebases build `Transcript(segments=..., language=...)` via a dataclass; use the real signature.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_pyannote_diariser.py -v`
Expected: FAIL — `diarise()` doesn't accept `mic_audio_path`/`system_audio_path`, no `_speaker_turns`.

- [ ] **Step 3: Rewrite the diariser as the hybrid**

Replace the body of `src/pyannote_diariser.py` from the `class PyAnnoteDiariser` line onward with:

```python
class PyAnnoteDiariser:
    """Hybrid diariser: energy decides me-vs-remote, pyannote separates the
    remote speakers.

    The mic channel is unambiguously the local user, so this runs the energy
    diariser first (mic-vs-system RMS) to mark the user's segments as
    ``speaker_name``, then runs pyannote over the *remote* (system) source
    WAV and overlays ``SPEAKER_NN`` onto every non-user segment. This keeps
    "Me" reliable while separating multiple remote participants.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._pipeline = None  # Lazy-loaded
        self._lock = threading.Lock()

    def _load_pipeline(self) -> None:
        """Lazy-load the pyannote pipeline (the model is gated — needs HF_TOKEN)."""
        from pyannote.audio import Pipeline

        if not os.environ.get("HF_TOKEN"):
            logger.warning(
                "HF_TOKEN not set — the gated pyannote model may fail to load; "
                "diarisation will degrade to the energy backend."
            )
        self._pipeline = Pipeline.from_pretrained(
            self._config.pyannote_model,
            use_auth_token=os.environ.get("HF_TOKEN"),
        )
        logger.info("Loaded pyannote pipeline: %s", self._config.pyannote_model)

    def _speaker_turns(self, audio_path: Path) -> list[tuple[float, float, str]]:
        """Run pyannote on *audio_path* and return (start, end, label) turns."""
        if self._pipeline is None:
            with self._lock:
                if self._pipeline is None:
                    self._load_pipeline()
        params: dict = {}
        if self._config.num_speakers > 0:
            params["num_speakers"] = self._config.num_speakers
        diarisation = self._pipeline(str(audio_path), **params)
        return [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in diarisation.itertracks(yield_label=True)
        ]

    def diarise(
        self,
        transcript,
        audio_path: Path,
        *,
        mic_audio_path: Path | None = None,
        system_audio_path: Path | None = None,
    ):
        """Label each segment: local user → ``speaker_name``; remote → SPEAKER_NN."""
        from src.diariser import EnergyDiariser

        me = self._config.speaker_name

        # Step 1: energy me/remote (only when the mic source survives).
        if mic_audio_path is not None and Path(mic_audio_path).exists():
            try:
                EnergyDiariser(self._config).diarise(
                    transcript, audio_path, mic_audio_path=mic_audio_path
                )
            except Exception as e:
                logger.warning("Energy pre-pass failed (%s); treating all as remote", e)
                for seg in transcript.segments:
                    seg.speaker = ""

        # Step 2: pyannote over the remote (system) channel.
        remote_wav = (
            system_audio_path
            if system_audio_path is not None and Path(system_audio_path).exists()
            else audio_path
        )
        turns = self._speaker_turns(Path(remote_wav))

        # Step 3: overlay SPEAKER_NN onto every non-user segment.
        for segment in transcript.segments:
            if segment.speaker == me:
                continue
            best_speaker, best_overlap = "", 0.0
            for turn_start, turn_end, speaker in turns:
                overlap = max(
                    0.0, min(segment.end, turn_end) - max(segment.start, turn_start)
                )
                if overlap > best_overlap:
                    best_overlap, best_speaker = overlap, speaker
            segment.speaker = best_speaker or self._config.remote_label

        counts: dict[str, int] = {}
        for seg in transcript.segments:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
        logger.info("Hybrid pyannote diarisation complete: %s", counts)
        return transcript
```

(Keep the module's existing imports: `logging`, `os`, `threading`, `Path`. Remove the now-unused `from src.diariser import DiarisationConfig` / `from src.transcriber import Transcript` top-level imports only if ruff flags them; the `EnergyDiariser` import stays function-local to avoid a circular import with `src.diariser`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pyannote_diariser.py -v && ruff check src/pyannote_diariser.py`
Expected: PASS, no lint findings.

- [ ] **Step 5: Commit**

```bash
git add src/pyannote_diariser.py tests/test_pyannote_diariser.py
git commit -m "feat(diarisation): hybrid pyannote-on-remote + energy me/remote split"
```

---

### Task 3: `create_diariser` degrades to energy instead of raising

**Files:**

- Modify: `src/diariser.py` (`create_diariser`, lines 143-161)
- Test: `tests/test_diariser.py`

**Interfaces:**

- Consumes: `DiarisationConfig`.
- Produces: `create_diariser(config)` returns an `EnergyDiariser` (logging a warning) when `config.backend == "pyannote"` but `pyannote.audio` cannot be imported, instead of raising `ValueError`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_diariser.py`:

```python
def test_create_diariser_degrades_to_energy_without_pyannote(monkeypatch):
    import builtins

    from src.diariser import EnergyDiariser, create_diariser
    from src.utils.config import DiarisationConfig

    real_import = builtins.__import__

    def _no_pyannote(name, *args, **kwargs):
        if name.startswith("pyannote"):
            raise ImportError("no pyannote")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pyannote)
    d = create_diariser(DiarisationConfig(backend="pyannote"))
    assert isinstance(d, EnergyDiariser)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_diariser.py::test_create_diariser_degrades_to_energy_without_pyannote -v`
Expected: FAIL — `create_diariser` raises `ValueError`.

- [ ] **Step 3: Degrade instead of raising**

In `src/diariser.py`, replace the `if backend == "pyannote":` block in `create_diariser` with:

```python
    if backend == "pyannote":
        try:
            import pyannote.audio  # noqa: F401
        except ImportError:
            logger.warning(
                "pyannote.audio unavailable — degrading diarisation to the "
                "energy backend (binary Me/Remote)."
            )
            return EnergyDiariser(config)
        from src.pyannote_diariser import PyAnnoteDiariser

        return PyAnnoteDiariser(config)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_diariser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/diariser.py tests/test_diariser.py
git commit -m "feat(diarisation): degrade to energy when pyannote is unavailable"
```

---

### Task 4: Pipeline passes source WAVs to pyannote + runtime degrade

**Files:**

- Modify: `src/pipeline_runner.py` (`_diarise`, ~lines 470-495)
- Test: `tests/test_pipeline_runner.py`

**Interfaces:**

- Consumes: `PyAnnoteDiariser.diarise(..., mic_audio_path=, system_audio_path=)` (Task 2), `EnergyDiariser`, `derive_source_paths(audio_path, temp_audio_dir)` (already in `src/pipeline_runner.py`).
- Produces: `_diarise` calls the pyannote backend with both source WAVs (system derived via `derive_source_paths`), and if the pyannote diarise call raises at runtime (model load / gated-model failure), it logs and re-runs with `EnergyDiariser` so the pipeline still gets Me/Remote labels.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline_runner.py` (match the file's runner/fakes idiom):

```python
def test_diarise_degrades_to_energy_when_pyannote_raises(pipeline_with_fakes, tmp_path):
    runner, _db = pipeline_with_fakes

    class _BoomPyannote:
        def diarise(self, transcript, audio_path, **kwargs):
            raise RuntimeError("model load failed")

    runner._diariser = _BoomPyannote()
    # A tiny transcript + fake source wavs so the energy fallback can run.
    t = _make_transcript_with_segments()
    sys_wav, mic_wav = _make_source_wavs(tmp_path)  # helper: writes _system/_mic
    runner._diarise(t, sys_wav, mic_wav, "m1")
    # Energy fallback labelled the segments (not left blank).
    assert any(seg.speaker for seg in t.segments)
```

If the file has no such helpers, drive `_diarise` directly with a fake diariser whose `diarise` raises, plus real tiny WAVs, and assert the fallback path ran (segments labelled and no exception propagated).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k diarise_degrades -v`
Expected: FAIL — the exception propagates / segments stay blank.

- [ ] **Step 3: Update `_diarise`**

In `src/pipeline_runner.py`, replace the `_diarise` method body (lines ~470-495) with:

```python
    def _diarise(
        self,
        transcript,
        audio_path: Path,
        mic_audio_path: Path | None,
        meeting_id: str | None,
    ) -> None:
        if not self._diariser:
            return
        logger.info("Running speaker diarisation...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="diarising")
        try:
            if isinstance(self._diariser, EnergyDiariser):
                self._diariser.diarise(transcript, audio_path, mic_audio_path=mic_audio_path)
            else:
                system_audio_path = derive_source_paths(
                    audio_path, self._config.audio.temp_audio_dir
                ).get("system")
                self._diariser.diarise(
                    transcript,
                    audio_path,
                    mic_audio_path=mic_audio_path,
                    system_audio_path=system_audio_path,
                )
        except Exception as e:
            logger.warning(
                "Diarisation backend failed (%s) — degrading to energy for this run", e
            )
            self._emit(
                "pipeline.warning",
                meeting_id=meeting_id,
                source="diarisation",
                message="Speaker separation unavailable; used basic Me/Remote labels.",
            )
            try:
                EnergyDiariser(self._config.diarisation).diarise(
                    transcript, audio_path, mic_audio_path=mic_audio_path
                )
            except Exception as e2:
                logger.warning("Energy fallback also failed: %s", e2)
```

(Confirm `derive_source_paths` and `EnergyDiariser` are already imported at the top of `pipeline_runner.py` — `EnergyDiariser` is imported at line 28; `derive_source_paths` is defined in this module. `self._config.audio.temp_audio_dir` is the temp dir the source WAVs live in.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k diaris -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline_runner.py tests/test_pipeline_runner.py
git commit -m "feat(diarisation): pass source WAVs to pyannote; degrade to energy at runtime"
```

---

### Task 5: Bundle pyannote in the daemon + guard test

**Files:**

- Modify: `context-recall.spec` (hidden imports / `collect_submodules`)
- Modify: `tests/test_spec_bundle_guards.py`

**Interfaces:**

- Consumes: nothing.
- Produces: the frozen daemon can `import pyannote.audio`; the guard test fails if `pyannote` hidden imports are dropped.

**Background:** `torch`/`torchaudio`/`speechbrain` are already bundled (voice-ID). pyannote adds `pyannote.audio`, `pyannote.core`, `pyannote.pipeline`, and helpers `asteroid_filterbanks` / `pytorch_metric_learning`. The model itself is **gated** (`HF_TOKEN` + accepted terms) and is downloaded at runtime, not bundled — so the guard test covers the _package_, and the runtime degrade (Task 4) covers a missing/unauthorised model.

- [ ] **Step 1: Write the failing guard assertions**

Add to `tests/test_spec_bundle_guards.py`:

```python
@pytest.mark.parametrize(
    "module",
    ['"pyannote.audio"', '"pyannote.core"', '"pyannote.pipeline"'],
)
def test_spec_bundles_pyannote(spec_text, module):
    assert module in spec_text, f"{module} missing from context-recall.spec hiddenimports"


def test_spec_collects_pyannote_submodules(spec_text):
    assert 'collect_submodules("pyannote.audio")' in spec_text
```

(Match the existing `spec_text` fixture in that file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_spec_bundle_guards.py -k pyannote -v`
Expected: FAIL — pyannote not in the spec.

- [ ] **Step 3: Add the hidden imports + submodule collection**

In `context-recall.spec`, add to `hiddenimports` (after the speechbrain/torch group):

```python
        # Neural diarisation (optional; degrades to the energy backend when
        # the gated model can't load — see src/pipeline_runner._diarise).
        "pyannote.audio",
        "pyannote.core",
        "pyannote.pipeline",
        "asteroid_filterbanks",
        "pytorch_metric_learning",
```

and add to the concatenated `collect_submodules(...)` expression (mirroring the EventKit/speechbrain entries):

```python
    + collect_submodules("pyannote.audio")
```

(Ensure the trailing comma/bracket keep the `hiddenimports=[...] + ...` expression valid — after editing run the parse check below.)

- [ ] **Step 4: Run the guard test + spec parse**

Run: `python3 -m pytest tests/test_spec_bundle_guards.py -v`
Then: `python3 -c "import ast; ast.parse(open('context-recall.spec').read()); print('ok')"`
Expected: PASS, prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add context-recall.spec tests/test_spec_bundle_guards.py
git commit -m "fix(build): bundle pyannote.audio for neural diarisation"
```

---

### Task 6: Speaker-correction panel (Alter pattern)

**Files:**

- Create: `ui/src/components/meetings/SpeakerPanel.tsx`
- Modify: `ui/src/components/meetings/MeetingDetail.tsx` (render the panel; pass segments + seek)
- Test: `ui/src/components/meetings/__tests__/SpeakerPanel.test.tsx` (new)

**Interfaces:**

- Consumes: `setSpeakerName(meetingId, speaker, name)` and `assignPersonToSpeaker` (existing in `ui/src/lib/api.ts`, already used by the inline editor + `AssignSpeakerMenu`), the `AudioSeekHandle.seekTo(seconds)` (from `AudioPlayer`), and the meeting's transcript segments.
- Produces: `<SpeakerPanel meetingId={string} segments={TranscriptSegment[]} onSeek={(s:number)=>void} />` — lists each detected speaker with its segment count and colour, a "Play" button that seeks to that speaker's first segment, and an inline rename that PATCHes via `setSpeakerName` and invalidates `["meeting", meetingId]`.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/meetings/__tests__/SpeakerPanel.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SpeakerPanel } from "../SpeakerPanel";
import { makeWrapper } from "../../../test/queryWrapper";

const segments = [
  { start: 0, end: 1, text: "hi", speaker: "SPEAKER_00" },
  { start: 1, end: 2, text: "hello", speaker: "SPEAKER_01" },
  { start: 2, end: 3, text: "bye", speaker: "SPEAKER_00" },
] as never[];

describe("SpeakerPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(
      async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("lists each detected speaker with its segment count", () => {
    render(<SpeakerPanel meetingId="m1" segments={segments} onSeek={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText("SPEAKER_00")).toBeInTheDocument();
    expect(screen.getByText("SPEAKER_01")).toBeInTheDocument();
    expect(screen.getByText(/2 segments/i)).toBeInTheDocument(); // SPEAKER_00
  });

  it("seeks to a speaker's first segment on Play", () => {
    const onSeek = vi.fn();
    render(<SpeakerPanel meetingId="m1" segments={segments} onSeek={onSeek} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getAllByRole("button", { name: /play .* segments/i })[1]); // SPEAKER_01
    expect(onSeek).toHaveBeenCalledWith(1);
  });

  it("renames a speaker via the API", async () => {
    render(<SpeakerPanel meetingId="m1" segments={segments} onSeek={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getAllByRole("button", { name: /rename/i })[0]);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Alice" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/meetings/m1/speakers/SPEAKER_00"),
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- SpeakerPanel`
Expected: FAIL — no `SpeakerPanel`.

- [ ] **Step 3: Implement the panel**

Create `ui/src/components/meetings/SpeakerPanel.tsx`:

```tsx
import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { setSpeakerName } from "../../lib/api";
import { useToast } from "../common/Toast";
import type { TranscriptSegment } from "../../lib/types";

/** Alter-style speaker-correction panel: list detected speakers, play each
 *  speaker's segments, and rename one (propagates across the transcript and
 *  persists via the speaker-mappings table). */
export function SpeakerPanel({
  meetingId,
  segments,
  onSeek,
}: {
  meetingId: string;
  segments: TranscriptSegment[];
  onSeek: (seconds: number) => void;
}) {
  const speakers = useMemo(() => {
    const map = new Map<string, { count: number; first: number }>();
    for (const s of segments) {
      if (!s.speaker) continue;
      const e = map.get(s.speaker);
      if (e) e.count += 1;
      else map.set(s.speaker, { count: 1, first: s.start });
    }
    return [...map.entries()].map(([speaker, v]) => ({ speaker, ...v }));
  }, [segments]);

  if (speakers.length === 0) return null;

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-4">
      <h2 className="text-sm font-medium text-text-primary">Speakers</h2>
      <p className="text-xs text-text-muted mt-0.5">
        Play a speaker's parts, then rename them — the change applies across the
        transcript and is kept when you reprocess.
      </p>
      <ul className="mt-3 flex flex-col gap-2">
        {speakers.map((s) => (
          <SpeakerRow
            key={s.speaker}
            meetingId={meetingId}
            speaker={s.speaker}
            count={s.count}
            firstStart={s.first}
            onSeek={onSeek}
          />
        ))}
      </ul>
    </div>
  );
}

function SpeakerRow({
  meetingId,
  speaker,
  count,
  firstStart,
  onSeek,
}: {
  meetingId: string;
  speaker: string;
  count: number;
  firstStart: number;
  onSeek: (seconds: number) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(speaker);

  const rename = useMutation({
    mutationFn: (next: string) => setSpeakerName(meetingId, speaker, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      setEditing(false);
    },
    onError: () => {
      toast.error("Failed to rename speaker.");
      setEditing(false);
    },
  });

  function commit() {
    const next = value.trim();
    if (!next || next === speaker) {
      setValue(speaker);
      setEditing(false);
      return;
    }
    rename.mutate(next);
  }

  return (
    <li className="flex items-center gap-2 text-sm">
      <button
        type="button"
        aria-label={`Play ${speaker} segments`}
        onClick={() => onSeek(firstStart)}
        className="px-2 py-1 text-xs rounded-lg bg-accent/10 text-accent hover:bg-accent/20"
      >
        ▶ Play
      </button>
      {editing ? (
        <input
          autoFocus
          value={value}
          disabled={rename.isPending}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              setValue(speaker);
              setEditing(false);
            }
          }}
          className="bg-surface border border-border rounded px-1 text-text-primary"
        />
      ) : (
        <span className="font-medium text-text-primary">{speaker}</span>
      )}
      <span className="text-xs text-text-muted">
        {count} segment{count === 1 ? "" : "s"}
      </span>
      {!editing && (
        <button
          type="button"
          aria-label={`Rename ${speaker}`}
          onClick={() => {
            setValue(speaker);
            setEditing(true);
          }}
          className="ml-auto px-2 py-1 text-xs rounded-lg text-text-secondary hover:text-text-primary"
        >
          Rename
        </button>
      )}
    </li>
  );
}
```

- [ ] **Step 4: Render it in MeetingDetail**

In `ui/src/components/meetings/MeetingDetail.tsx`, import the panel near the other meeting imports:

```tsx
import { SpeakerPanel } from "./SpeakerPanel";
```

Then, immediately before the transcript block is rendered (find where segments are mapped — grep for `uniqueSpeakers` / `segments.map` in the file; render the panel just above that section), add:

```tsx
{
  hasAudio && (
    <SpeakerPanel
      meetingId={meeting.id}
      segments={segments}
      onSeek={(s) => audioSeekRef.current?.seekTo(s)}
    />
  );
}
```

(`segments`, `hasAudio`, `meeting`, and `audioSeekRef` already exist in this component — reuse them. If `hasAudio` is not in scope at that point, render unconditionally; the Play button just seeks a possibly-absent player, which is a no-op.)

- [ ] **Step 5: Run the tests + type-check**

Run: `cd ui && npm test -- SpeakerPanel && npx tsc --noEmit`
Expected: PASS, no type errors. If `setSpeakerName`'s signature differs from `(meetingId, speaker, name)`, read `ui/src/lib/api.ts` and match it in the mutation.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/meetings/SpeakerPanel.tsx ui/src/components/meetings/MeetingDetail.tsx ui/src/components/meetings/__tests__/SpeakerPanel.test.tsx
git commit -m "feat(ui): speaker-correction panel with per-speaker playback + rename"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass. If a pre-existing diariser/orchestrator test assumed diarisation was off or `energy` by default, fix it to set `DiarisationConfig(backend="energy")` (or `enabled=False`) explicitly for that case.

- [ ] **Step 2: Python lint + spec parse**

Run: `ruff check src/ tests/ && python3 -c "import ast; ast.parse(open('context-recall.spec').read()); print('ok')"`
Expected: clean, `ok`.

- [ ] **Step 3: UI tests + type-check**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit any verification fixes**

```bash
git add -A
git commit -m "test(diarisation): fix assertions surfaced by the pyannote default"
```

---

## Manual verification (not automatable in CI — needs a signed build + HF token)

1. Set `HF_TOKEN` (with the `pyannote/speaker-diarization-3.1` terms accepted) and record a meeting with 3+ remote participants → the transcript shows `SPEAKER_00…N` (not a single "Remote"); the daemon logs "Hybrid pyannote diarisation complete".
2. Without `HF_TOKEN` / offline → the model fails to load and diarisation degrades to energy (Me/Remote), with a `pipeline.warning` surfaced in the UI; the pipeline still completes.
3. In meeting detail, the Speaker panel lists the detected speakers; "Play" jumps to each speaker's first segment; renaming a speaker updates every one of their segments and persists across a reprocess.
4. With enrolled voice profiles, recurring speakers are auto-named instead of `SPEAKER_NN`.

---

## Self-Review

- **Spec coverage:** pyannote on the remote channel + mic="Me" → Task 2 (hybrid); backend `energy`|`pyannote` defaulting to pyannote → Task 1; degrade when torch/pyannote/model absent → Tasks 3 (import) + 4 (runtime) + 2 (missing WAV fallback); bundle deps + model-ships/downloads → Task 5 (deps bundled; gated model downloads at runtime via `HF_TOKEN`, degrade covers absence); voice-ID naming → reused (existing `_identify_voices`, `VoiceRecogniser` matches `SPEAKER_NN`); calendar-attendee seeding → reused (existing `_enrich_speakers_from_attendees`); Alter correction panel (list speakers, play segments, reassign/rename, propagate, persist, reprocess-safe) → Task 6 (UI) + existing `set_speaker_name`/`_reapply_speaker_mappings`. No migration (speaker_mappings already exists).
- **Placeholder scan:** none — each code/test step is concrete; host-file insertion points (MeetingDetail transcript slot, `pipeline_runner` imports, the spec concat expression, repo/test fixtures) name the exact grep + change because those spots vary and inventing their surroundings would be wrong.
- **Type consistency:** `diarise(..., mic_audio_path=, system_audio_path=)` and `_speaker_turns(audio_path)` are defined in Task 2 and called identically in Task 4; `create_diariser` returns a `DiariserBackend` in Task 3 used by the pipeline in Task 4; `SpeakerPanel` props (`meetingId`, `segments`, `onSeek`) defined in Task 6 match its MeetingDetail usage; `setSpeakerName(meetingId, speaker, name)` is the existing signature reused in Task 6.
