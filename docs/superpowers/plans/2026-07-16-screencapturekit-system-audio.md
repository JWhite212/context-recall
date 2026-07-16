# ScreenCaptureKit System-Audio Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a ScreenCaptureKit (SCK) system-audio capture backend, auto-selected by default, so the daemon recovers meeting audio despite the macOS 26.6-beta Microphone-TCC defect that zeros the BlackHole loopback.

**Architecture:** A signed Swift helper binary (built from the proven `sck_probe.swift`) captures system audio through the **Screen Recording** TCC service and writes `meeting_<ts>_system.wav` as 16 kHz mono PCM-16 — byte-identical to today's BlackHole output. A new `src/system_audio.py` abstracts the "system source" into two backends (BlackHole / SCK) behind one interface; `AudioCapture` drives the selected backend while its mic capture, RMS-normalised merge, diarisation, and the rest of the pipeline stay unchanged.

**Tech Stack:** Python 3.12 (daemon, `sounddevice`, `soundfile`, `numpy`, FastAPI, pytest), Swift 6 / ScreenCaptureKit + AVFoundation (helper), PyInstaller + `codesign` (bundling), bash (build scripts).

## Global Constraints

- **Platform:** macOS + Apple Silicon only. SCK requires macOS 13+.
- **`_system.wav` format contract:** 16 kHz mono PCM-16 (`config.audio.sample_rate`, 1 channel). Both backends MUST write exactly this so `AudioCapture._merge_dual_source`'s "same samplerate/channels" guard passes and no merge/diarise code changes.
- **Bundle identifiers:** daemon `dev.jamiewhite.contextrecall.daemon`; SCK helper `dev.jamiewhite.contextrecall.sck`.
- **Signing:** inside-out — sign the nested helper **before** the outer `.app` seal. Stable self-signed cert when present (`CONTEXT_RECALL_SIGN_IDENTITY` override, else `security find-certificate -c "Context Recall Self-Signed"`), ad-hoc `-` fallback for CI/fresh clones. Never hard-fail the build on a missing cert or missing `swiftc`.
- **Frozen no-.pyc rule:** do not introduce imports that write bytecode into the signed bundle at runtime (already enforced by `src/utils/frozen_runtime.disable_bytecode_writing`).
- **Tests never touch real SCK or real devices:** pytest fakes the helper subprocess and mocks `sounddevice`. The native helper smoke test is manual/local (Apple Silicon + a Screen Recording grant), like the existing MLX/Tauri Apple-Silicon-only CI jobs.
- **Helper CLI contract (shared by the Swift binary, the Python driver, and the test stub):**
  - `--output <path>`: capture system audio → 16 kHz mono PCM-16 WAV at `<path>`; on `SIGTERM`/`SIGINT` finalise the WAV and `exit(0)`; while capturing, print `rms=<float>\n` to stdout ~10×/s.
  - `--check-permission`: print exactly one line, `granted` or `denied`, then `exit(0)`.
  - On unrecoverable error (Screen Recording denied at capture time, SCK failure): print `error=<reason>\n` to stderr and exit non-zero.

---

### Task 1: Config field `system_capture_backend`

**Files:**

- Modify: `src/utils/config.py` (`AudioConfig`, lines 78–109)
- Test: `tests/test_config.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `AudioConfig.system_capture_backend: str` (default `"auto"`; one of `"auto" | "blackhole" | "screencapturekit"`), validated in `__post_init__`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_audio_config_system_capture_backend_default():
    from src.utils.config import AudioConfig
    assert AudioConfig().system_capture_backend == "auto"


def test_audio_config_system_capture_backend_accepts_known_values():
    from src.utils.config import AudioConfig
    for value in ("auto", "blackhole", "screencapturekit"):
        assert AudioConfig(system_capture_backend=value).system_capture_backend == value


def test_audio_config_system_capture_backend_rejects_unknown():
    import pytest
    from src.utils.config import AudioConfig
    with pytest.raises(ValueError, match="system_capture_backend"):
        AudioConfig(system_capture_backend="coreaudio")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -k system_capture_backend -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'system_capture_backend'`.

- [ ] **Step 3: Add the field and validation**

In `src/utils/config.py`, add the field to `AudioConfig` (place it just after `silence_alert_threshold` on line 102):

```python
    # System-audio capture backend. "auto" prefers ScreenCaptureKit when the
    # OS supports it (macOS 13+) and the signed helper is bundled, else the
    # BlackHole loopback. "blackhole" / "screencapturekit" force a backend.
    # SCK captures system output via the Screen Recording TCC service, which
    # keeps working on macOS betas where the Microphone service (and thus the
    # BlackHole input) is broken.
    system_capture_backend: str = "auto"
```

Extend the existing `__post_init__` (currently lines 104–109) so it also validates the new field:

```python
    def __post_init__(self) -> None:
        if not (1e-7 <= self.silence_alert_threshold <= 1e-2):
            raise ValueError(
                "silence_alert_threshold must be between 1e-7 and 1e-2, "
                f"got {self.silence_alert_threshold!r}"
            )
        valid_backends = {"auto", "blackhole", "screencapturekit"}
        if self.system_capture_backend not in valid_backends:
            raise ValueError(
                "system_capture_backend must be one of "
                f"{sorted(valid_backends)}, got {self.system_capture_backend!r}"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -k system_capture_backend -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Document the option in the config template**

In `config.example.yaml`, under the `audio:` section, add:

```yaml
# System-audio backend: auto | blackhole | screencapturekit.
# "auto" uses ScreenCaptureKit when available (recovers meeting audio even
# when the microphone permission is broken), else the BlackHole loopback.
system_capture_backend: auto
```

- [ ] **Step 6: Commit**

```bash
git add src/utils/config.py tests/test_config.py config.example.yaml
git commit -m "feat(config): add audio.system_capture_backend (auto|blackhole|screencapturekit)"
```

---

### Task 2: Swift SCK helper binary + build script + de-risk spike

**Files:**

- Create: `macos/sck-audio-capture/main.swift`
- Create: `scripts/build_sck_helper.sh`
- Verify (manual/local): native smoke test + daemon-child attribution spike

**Interfaces:**

- Consumes: nothing.
- Produces: an executable `sck-audio-capture` honouring the **Helper CLI contract** in Global Constraints. Dev build output path: `macos/sck-audio-capture/.build/sck-audio-capture`.

> **This task is the highest-risk part of the project — do it first and confirm the spike before building the Python integration.** The risk is Screen Recording TCC _attribution_ for a launchd-daemon-spawned child (see Step 7). If the spike fails, stop and re-evaluate the helper's identity/signing before continuing.

- [ ] **Step 1: Write the helper source**

Create `macos/sck-audio-capture/main.swift`:

```swift
// sck-audio-capture — capture macOS system audio to a 16 kHz mono PCM-16 WAV.
//
// Usage:
//   sck-audio-capture --output <path.wav>   capture until SIGTERM/SIGINT, then finalise
//   sck-audio-capture --check-permission     print "granted" | "denied", exit 0
//
// While capturing, prints `rms=<float>` to stdout ~10x/sec for level metering.
// On Screen Recording denial / SCK failure, prints `error=...` to stderr and
// exits non-zero. Captures SYSTEM OUTPUT only (never the microphone) via the
// Screen Recording TCC service — the escape hatch for macOS builds where the
// Microphone service is broken.
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreGraphics
import Darwin

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("error=\(message)\n".data(using: .utf8)!)
    exit(1)
}

final class SystemAudioCapturer: NSObject, SCStreamOutput, SCStreamDelegate {
    private let outURL: URL
    private let sampleRate: Double
    private let outFormat: AVAudioFormat
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private var lastEmit = Date(timeIntervalSince1970: 0)

    init(outputPath: String, sampleRate: Double) {
        self.outURL = URL(fileURLWithPath: outputPath)
        self.sampleRate = sampleRate
        self.outFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: true
        )!
        super.init()
    }

    func openFile() throws {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        self.file = try AVAudioFile(
            forWriting: outURL,
            settings: settings,
            commonFormat: .pcmFormatInt16,
            interleaved: true
        )
    }

    func finalizeAndExit() {
        self.file = nil  // AVAudioFile finalises the WAV header on release.
        exit(0)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        guard let fmtDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc) else { return }
        var asbd = asbdPtr.pointee
        guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else { return }

        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0,
              let srcBuffer = AVAudioPCMBuffer(
                  pcmFormat: srcFormat,
                  frameCapacity: AVAudioFrameCount(numSamples)) else { return }
        srcBuffer.frameLength = AVAudioFrameCount(numSamples)
        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(numSamples),
            into: srcBuffer.mutableAudioBufferList)
        guard copyStatus == noErr else { return }

        if converter == nil {
            converter = AVAudioConverter(from: srcFormat, to: outFormat)
        }
        guard let converter = converter else { return }

        let ratio = outFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(numSamples) * ratio) + 32
        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: outFormat, frameCapacity: outCapacity) else { return }

        var err: NSError?
        var provided = false
        converter.convert(to: outBuffer, error: &err) { _, statusPtr in
            if provided { statusPtr.pointee = .noDataNow; return nil }
            provided = true
            statusPtr.pointee = .haveData
            return srcBuffer
        }
        if err != nil || outBuffer.frameLength == 0 { return }

        do {
            if file == nil { try openFile() }
            try file?.write(from: outBuffer)
        } catch {
            fail("write failed: \(error)")
        }
        emitRMS(outBuffer)
    }

    private func emitRMS(_ buffer: AVAudioPCMBuffer) {
        let now = Date()
        guard now.timeIntervalSince(lastEmit) >= 0.1 else { return }
        lastEmit = now
        guard let ch = buffer.int16ChannelData else { return }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return }
        var sumSq = 0.0
        for i in 0..<n {
            let v = Double(ch[0][i]) / 32768.0
            sumSq += v * v
        }
        let rms = (sumSq / Double(n)).squareRoot()
        print("rms=\(String(format: "%.6f", rms))")
        fflush(stdout)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("stream stopped: \(error)")
    }
}

func firstDisplayFilter() async throws -> SCContentFilter {
    let content = try await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: true)
    guard let display = content.displays.first else { fail("no display available") }
    return SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
}

func makeConfig(sampleRate: Int) -> SCStreamConfiguration {
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true   // don't capture our own output
    config.sampleRate = 48000                    // SCK native; we downsample to 16k
    config.channelCount = 2
    config.width = 2; config.height = 2          // SCK needs a video config even audio-only
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    return config
}

func runCapture(outputPath: String, sampleRate: Int) async {
    do {
        let filter = try await firstDisplayFilter()
        let capturer = SystemAudioCapturer(outputPath: outputPath, sampleRate: Double(sampleRate))
        let stream = SCStream(filter: filter, configuration: makeConfig(sampleRate: sampleRate),
                              delegate: capturer)
        try stream.addStreamOutput(capturer, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "sck.audio"))

        // Finalise cleanly on SIGTERM/SIGINT (the daemon stops us this way).
        for sig in [SIGTERM, SIGINT] { signal(sig, SIG_IGN) }
        for sig in [SIGTERM, SIGINT] {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler {
                Task { try? await stream.stopCapture(); capturer.finalizeAndExit() }
            }
            src.resume()
            // Keep the source alive for the process lifetime.
            signalSources.append(src)
        }
        try await stream.startCapture()
        // Park forever; the signal handler exits the process.
        try await Task.sleep(nanoseconds: .max)
    } catch {
        fail("\(error)")
    }
}

var signalSources: [DispatchSourceSignal] = []

func checkPermission() {
    // CGPreflight returns true only when Screen Recording is granted.
    // It cannot distinguish "undetermined" from "denied", so report both as denied.
    print(CGPreflightScreenCaptureAccess() ? "granted" : "denied")
    exit(0)
}

// ---- Argument parsing ----
let args = CommandLine.arguments
if args.contains("--check-permission") {
    checkPermission()
}
guard let outIdx = args.firstIndex(of: "--output"), outIdx + 1 < args.count else {
    fail("usage: sck-audio-capture --output <path.wav> | --check-permission")
}
let outputPath = args[outIdx + 1]
var sampleRate = 16000
if let srIdx = args.firstIndex(of: "--sample-rate"), srIdx + 1 < args.count,
   let sr = Int(args[srIdx + 1]) {
    sampleRate = sr
}

let sem = DispatchSemaphore(value: 0)
Task { await runCapture(outputPath: outputPath, sampleRate: sampleRate); sem.signal() }
// runCapture only returns on error (success exits via signal handler).
RunLoop.main.run()
```

- [ ] **Step 2: Write the dev build script**

Create `scripts/build_sck_helper.sh`:

```bash
#!/bin/bash
# Compile the ScreenCaptureKit system-audio helper for local (non-frozen) runs.
# The daemon resolves it at macos/sck-audio-capture/.build/sck-audio-capture.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$ROOT/macos/sck-audio-capture/main.swift"
OUT_DIR="$ROOT/macos/sck-audio-capture/.build"
OUT="$OUT_DIR/sck-audio-capture"

if ! command -v swiftc >/dev/null 2>&1; then
    echo "ERROR: swiftc not found (install Xcode command-line tools)" >&2
    exit 1
fi
mkdir -p "$OUT_DIR"
echo "==> Compiling $SRC"
swiftc -O "$SRC" -o "$OUT"
echo "==> Built $OUT"
```

Then:

```bash
chmod +x scripts/build_sck_helper.sh
```

- [ ] **Step 3: Compile the helper**

Run: `./scripts/build_sck_helper.sh`
Expected: `==> Built .../macos/sck-audio-capture/.build/sck-audio-capture` and exit 0. (If `swiftc` reports a compile error, fix the source before proceeding — do not continue to the spike.)

- [ ] **Step 4: Native smoke test — `--check-permission`**

Run: `./macos/sck-audio-capture/.build/sck-audio-capture --check-permission`
Expected: prints `granted` or `denied` and exits 0. If `denied`, grant this terminal Screen Recording (System Settings → Privacy & Security → Screen Recording) and re-run until `granted` before the capture test.

- [ ] **Step 5: Native smoke test — capture real audio**

Run (plays TTS, captures ~4 s, stops with SIGTERM, checks the WAV):

```bash
HELPER=./macos/sck-audio-capture/.build/sck-audio-capture
OUT=$(mktemp -u).wav
say "testing system audio capture one two three four" &
"$HELPER" --output "$OUT" & HPID=$!
sleep 4
kill -TERM "$HPID"; wait "$HPID" 2>/dev/null || true
python3 - "$OUT" <<'PY'
import sys, soundfile as sf, numpy as np
data, sr = sf.read(sys.argv[1], dtype="float32")
rms = float(np.sqrt(np.mean(data**2))) if len(data) else 0.0
db = 20*np.log10(rms) if rms > 1e-9 else -100.0
print(f"sr={sr} frames={len(data)} rms_db={db:.1f}")
assert sr == 16000, f"expected 16kHz, got {sr}"
assert len(data) > 16000, "expected >1s of audio"
assert db > -60, f"expected real audio, got {db:.1f} dBFS"
print("SMOKE TEST PASS")
PY
```

Expected: `sr=16000`, non-trivial frame count, `rms_db` well above −60, `SMOKE TEST PASS`.

- [ ] **Step 6: Sign the helper with the stable identity (if present)**

```bash
HELPER=./macos/sck-audio-capture/.build/sck-audio-capture
if security find-certificate -c "Context Recall Self-Signed" >/dev/null 2>&1; then
    codesign --force --sign "Context Recall Self-Signed" \
        --identifier dev.jamiewhite.contextrecall.sck --timestamp=none "$HELPER"
else
    codesign --force --sign - --identifier dev.jamiewhite.contextrecall.sck "$HELPER"
fi
codesign --verify --verbose=1 "$HELPER"
```

Expected: `--verify` passes. Re-run Step 4 to confirm the signed binary still reports its permission (its Screen Recording grant is now pinned to the cert-leaf DR; a fresh grant may be requested once).

- [ ] **Step 7: De-risk spike — run the helper AS A DAEMON CHILD**

The production daemon spawns the helper; TCC attributes Screen Recording to the helper's own code identity. Confirm this works when the parent is the launchd daemon, not a terminal:

```bash
# With the installed daemon running, spawn the helper from inside it via the
# daemon's own process using the diagnostics shell, OR spawn from a launchd-
# owned context. Minimal check: verify the SIGNED helper prompts/captures when
# its responsible process is not Terminal.
launchctl asuser "$(id -u)" ./macos/sck-audio-capture/.build/sck-audio-capture --check-permission
```

Expected: prints `granted` after a one-time System Settings → Screen Recording grant for `sck-audio-capture`. **If it stays `denied` after granting, or no entry appears in the Screen Recording list, STOP** — the attribution assumption is wrong; record findings and re-evaluate (helper identifier / embedding it under the daemon bundle) before Task 3+. If `granted`, the design holds.

- [ ] **Step 8: Commit**

```bash
git add macos/sck-audio-capture/main.swift scripts/build_sck_helper.sh
git commit -m "feat(sck): Swift ScreenCaptureKit system-audio helper + dev build script"
```

---

### Task 3: `resolve_helper_path()` in `src/system_audio.py`

**Files:**

- Create: `src/system_audio.py`
- Test: `tests/test_system_audio.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `resolve_helper_path() -> Path | None` and module constant `HELPER_NAME = "sck-audio-capture"`. Returns the bundled helper path when frozen (`…/Context Recall Daemon.app/Contents/Resources/sck-audio-capture`), the dev build path otherwise (`<repo>/macos/sck-audio-capture/.build/sck-audio-capture`), or `None` when the binary is absent / not executable.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_system_audio.py`:

```python
"""Tests for the system-audio backend abstraction."""

import os
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
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(exe)):
        assert sa.resolve_helper_path() == helper


def test_resolve_helper_path_frozen_missing(tmp_path):
    macos = tmp_path / "App.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "context-recall-daemon"
    _make_exec(exe)
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", str(exe)):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_system_audio.py -k resolve_helper_path -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.system_audio'`.

- [ ] **Step 3: Create the module with `resolve_helper_path`**

Create `src/system_audio.py`:

```python
"""System-audio capture backends (BlackHole loopback / ScreenCaptureKit).

The daemon captures *system output* (remote meeting participants) through one
of two interchangeable backends, both writing ``meeting_<ts>_system.wav`` as
16 kHz mono PCM-16. ScreenCaptureKit uses the Screen Recording TCC service,
which keeps working on macOS betas where the Microphone service (and thus the
BlackHole input) is broken. See
docs/superpowers/specs/2026-07-16-screencapturekit-system-audio-design.md.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HELPER_NAME = "sck-audio-capture"


def resolve_helper_path() -> Path | None:
    """Locate the bundled/dev SCK helper binary, or None if unavailable.

    Frozen (.app) builds ship it at Contents/Resources/<HELPER_NAME>; dev runs
    use the output of scripts/build_sck_helper.sh. Returns None when the binary
    is missing or not executable, so callers can degrade to BlackHole.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent.parent / "Resources" / HELPER_NAME
    else:
        candidate = (
            Path(__file__).resolve().parent.parent
            / "macos"
            / "sck-audio-capture"
            / ".build"
            / HELPER_NAME
        )
    if candidate.exists() and os.access(candidate, os.X_OK):
        return candidate
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_system_audio.py -k resolve_helper_path -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/system_audio.py tests/test_system_audio.py
git commit -m "feat(system-audio): resolve_helper_path() for the SCK helper binary"
```

---

### Task 4: `SystemAudioBackend` base + `BlackHoleSystemCapture`

**Files:**

- Modify: `src/system_audio.py`
- Test: `tests/test_system_audio.py`

**Interfaces:**

- Consumes: `AudioCaptureError` (from `src.audio_capture`), `AudioConfig`, `resolve_helper_path` (Task 3).
- Produces:
  - `class SystemAudioBackend` with settable attrs `on_audio_data: Callable[[np.ndarray], None] | None`, `on_stream_status: Callable[[str, str], None] | None`, `last_error: AudioCaptureError | None`, `last_warning: str | None`; read-only property `latest_rms: float`; and methods `start(self, output_path: Path) -> None` / `stop(self) -> None` (both raise `NotImplementedError` in the base).
  - `class BlackHoleSystemCapture(SystemAudioBackend)` — `__init__(self, config: AudioConfig)`; resolves the BlackHole device internally, opens the system WAV + `sd.InputStream`, writes the file, and drives `latest_rms` / `on_audio_data` / `on_stream_status` exactly as the current `AudioCapture` system callback does.

> **Import-cycle note:** `system_audio.py` imports `AudioCaptureError` from `audio_capture.py` at module top. `audio_capture.py` must import `system_audio` **lazily** (inside `_record_loop`/`start`, done in Task 7), never at module top, so there is no cycle.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_system_audio.py`:

```python
import numpy as np
import soundfile as sf
from unittest.mock import MagicMock
from src.utils.config import AudioConfig
from src.audio_capture import AudioCaptureError

BH_DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2},
    {"name": "MacBook Pro Mic", "max_input_channels": 1},
]


def test_blackhole_backend_finds_device_and_opens_stream(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    out = tmp_path / "meeting_x_system.wav"
    with patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES), \
         patch("src.system_audio.sd.InputStream") as MockStream, \
         patch("src.system_audio.sf.SoundFile") as MockFile:
        backend.start(out)
        # Opened an input stream on the BlackHole index (0).
        assert MockStream.call_args.kwargs["device"] == 0
        MockStream.return_value.start.assert_called_once()
        backend.stop()
        MockStream.return_value.stop.assert_called_once()
        MockStream.return_value.close.assert_called_once()


def test_blackhole_backend_missing_device_sets_error(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    out = tmp_path / "meeting_x_system.wav"
    with patch("src.system_audio.sd.query_devices",
               return_value=[{"name": "MacBook Pro Mic", "max_input_channels": 1}]):
        backend.start(out)
    assert backend.last_error is not None
    assert isinstance(backend.last_error, AudioCaptureError)


def test_blackhole_backend_callback_forwards_data_and_rms(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    received = []
    backend.on_audio_data = received.append
    out = tmp_path / "meeting_x_system.wav"
    with patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES), \
         patch("src.system_audio.sd.InputStream") as MockStream, \
         patch("src.system_audio.sf.SoundFile") as MockFile:
        backend.start(out)
        # Grab the callback sd.InputStream was constructed with and drive it.
        cb = MockStream.call_args.kwargs["callback"]
        stereo = np.full((1024, 2), 0.5, dtype="float32")
        cb(stereo, 1024, None, None)
        assert len(received) == 1
        assert received[0].ndim == 1            # downmixed to mono
        assert backend.latest_rms > 0.0
        MockFile.return_value.write.assert_called()  # wrote mono to the file
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_system_audio.py -k blackhole -v`
Expected: FAIL — `AttributeError: module 'src.system_audio' has no attribute 'BlackHoleSystemCapture'`.

- [ ] **Step 3: Implement the base + BlackHole backend**

In `src/system_audio.py`, extend the imports and append the classes:

```python
import numpy as np
import sounddevice as sd
import soundfile as sf

from src.audio_capture import AudioCaptureError
from src.utils.config import AudioConfig
from typing import Callable
```

```python
class SystemAudioBackend:
    """Interface for a swappable system-audio source writing _system.wav.

    Subclasses own device/helper lifecycle and expose the current RMS plus
    forwarded live-audio / stream-status callbacks. AudioCapture drives one of
    these while owning the mic stream, merge, and pipeline.
    """

    def __init__(self) -> None:
        self.on_audio_data: Callable[[np.ndarray], None] | None = None
        self.on_stream_status: Callable[[str, str], None] | None = None
        self.last_error: AudioCaptureError | None = None
        self.last_warning: str | None = None
        self._latest_rms: float = 0.0

    @property
    def latest_rms(self) -> float:
        return self._latest_rms

    def start(self, output_path: Path) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class BlackHoleSystemCapture(SystemAudioBackend):
    """System source backed by the BlackHole loopback via sounddevice.

    Behaviourally identical to the pre-refactor AudioCapture system path: opens
    a 2ch float32 InputStream on the BlackHole device, downmixes to mono, writes
    16 kHz mono PCM-16, and forwards live audio + RMS.
    """

    def __init__(self, config: AudioConfig) -> None:
        super().__init__()
        self._config = config
        self._stream: sd.InputStream | None = None
        self._file: sf.SoundFile | None = None
        self._running = False

    def _find_blackhole(self, name: str) -> int:
        devices = sd.query_devices()
        for idx, device in enumerate(devices):
            if name.lower() in device["name"].lower() and device["max_input_channels"] > 0:
                logger.info("Found BlackHole device: '%s' (index %d)", device["name"], idx)
                return idx
        raise AudioCaptureError(f"BlackHole device '{name}' not found")

    def start(self, output_path: Path) -> None:
        try:
            idx = self._find_blackhole(self._config.blackhole_device_name)
        except AudioCaptureError as e:
            logger.error("BlackHole capture unavailable: %s", e)
            self.last_error = e
            return

        self._file = sf.SoundFile(
            str(output_path), mode="w",
            samplerate=self._config.sample_rate, channels=1, subtype="PCM_16",
        )
        self._running = True

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning("System audio status: %s", status)
                if self.on_stream_status is not None:
                    try:
                        self.on_stream_status("system", str(status))
                    except Exception:
                        pass
            if not self._running:
                return
            mono = indata.copy() if indata.ndim == 1 else np.mean(indata, axis=1)
            self._file.write(mono)
            if self.on_audio_data is not None:
                try:
                    self.on_audio_data(mono)
                except Exception:
                    pass
            self._latest_rms = float(np.sqrt(np.mean(mono**2)))

        self._stream = sd.InputStream(
            device=idx, samplerate=self._config.sample_rate,
            channels=2, dtype="float32", callback=callback, blocksize=1024,
        )
        self._stream.start()
        logger.info("BlackHole system capture started -> %s", output_path)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_system_audio.py -k "blackhole or resolve_helper_path" -v`
Expected: PASS (all backend + resolver tests green).

- [ ] **Step 5: Commit**

```bash
git add src/system_audio.py tests/test_system_audio.py
git commit -m "feat(system-audio): SystemAudioBackend base + BlackHoleSystemCapture"
```

---

### Task 5: `ScreenCaptureKitSystemCapture` driver

**Files:**

- Modify: `src/system_audio.py`
- Test: `tests/test_system_audio.py`

**Interfaces:**

- Consumes: `SystemAudioBackend` (Task 4), `AudioCaptureError`, `AudioConfig`.
- Produces: `class ScreenCaptureKitSystemCapture(SystemAudioBackend)` — `__init__(self, config: AudioConfig, helper_path: Path)`; `start(output_path)` spawns the helper (`--output`), reads `rms=` lines on a daemon thread to update `latest_rms`; `stop()` sends `SIGTERM`, waits ≤30 s, and on non-zero exit sets `last_error`/`last_warning` from stderr; `preflight() -> str` runs `--check-permission` and returns `"granted"`/`"denied"`/`"unknown"`.

- [ ] **Step 1: Write the failing tests (with a faked helper stub)**

Add to `tests/test_system_audio.py`:

```python
import textwrap


def _write_stub_helper(tmp_path, body_python) -> Path:
    """Create an executable python 'helper' honouring the CLI contract."""
    helper = tmp_path / "stub-helper"
    helper.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body_python))
    helper.chmod(0o755)
    return helper


CAPTURE_STUB = '''
    import sys, signal, time
    if "--check-permission" in sys.argv:
        print("granted"); sys.exit(0)
    out = sys.argv[sys.argv.index("--output") + 1]
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    # Write a tiny valid 16k mono PCM16 WAV up front.
    import soundfile as sf, numpy as np
    sf.write(out, np.zeros(16000, dtype="float32"), 16000, subtype="PCM_16")
    while not stop["v"]:
        print("rms=0.010000", flush=True)
        time.sleep(0.05)
    sys.exit(0)
'''

ERROR_STUB = '''
    import sys
    if "--check-permission" in sys.argv:
        print("denied"); sys.exit(0)
    sys.stderr.write("error=screen recording denied\\n")
    sys.exit(3)
'''


def test_sck_preflight_reports_granted(tmp_path):
    helper = _write_stub_helper(tmp_path, CAPTURE_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    assert backend.preflight() == "granted"


def test_sck_capture_updates_rms_and_finalises(tmp_path):
    helper = _write_stub_helper(tmp_path, CAPTURE_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    out = tmp_path / "meeting_x_system.wav"
    backend.start(out)
    # Give the reader thread a moment to parse an rms= line.
    for _ in range(40):
        if backend.latest_rms > 0:
            break
        time.sleep(0.05)
    backend.stop()
    assert backend.latest_rms > 0.0
    assert out.exists()
    assert backend.last_error is None


def test_sck_nonzero_exit_sets_error(tmp_path):
    helper = _write_stub_helper(tmp_path, ERROR_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    out = tmp_path / "meeting_x_system.wav"
    backend.start(out)
    backend.stop()
    assert backend.last_error is not None
    assert "screen recording" in (backend.last_warning or "").lower()
```

(Add `import time` at the top of the test module if not already present.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_system_audio.py -k sck -v`
Expected: FAIL — `AttributeError: module 'src.system_audio' has no attribute 'ScreenCaptureKitSystemCapture'`.

- [ ] **Step 3: Implement the SCK driver**

In `src/system_audio.py`, add imports and the class:

```python
import signal
import subprocess
import threading
```

```python
class ScreenCaptureKitSystemCapture(SystemAudioBackend):
    """System source backed by the signed Swift SCK helper subprocess.

    Captures system OUTPUT via the Screen Recording TCC service — the escape
    hatch for macOS builds where the Microphone service zeros the BlackHole
    input. Never captures the microphone.
    """

    _STOP_TIMEOUT = 30.0

    def __init__(self, config: AudioConfig, helper_path: Path) -> None:
        super().__init__()
        self._config = config
        self._helper = helper_path
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def preflight(self) -> str:
        try:
            result = subprocess.run(
                [str(self._helper), "--check-permission"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.warning("SCK preflight failed: %s", e)
            return "unknown"
        token = (result.stdout or "").strip().splitlines()
        value = token[-1] if token else ""
        return value if value in ("granted", "denied") else "unknown"

    def start(self, output_path: Path) -> None:
        try:
            self._proc = subprocess.Popen(
                [str(self._helper), "--output", str(output_path),
                 "--sample-rate", str(self._config.sample_rate)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except Exception as e:
            self.last_error = AudioCaptureError(f"Failed to launch SCK helper: {e}")
            logger.error("%s", self.last_error)
            return
        self._reader = threading.Thread(
            target=self._read_levels, name="sck-rms-reader", daemon=True)
        self._reader.start()
        logger.info("ScreenCaptureKit system capture started -> %s", output_path)

    def _read_levels(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("rms="):
                try:
                    self._latest_rms = float(line[4:])
                except ValueError:
                    pass

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=self._STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("SCK helper did not exit in %.0fs — killing", self._STOP_TIMEOUT)
            proc.kill()
            proc.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        stderr = ""
        try:
            if proc.stderr is not None:
                stderr = proc.stderr.read() or ""
        except Exception:
            pass
        if proc.returncode not in (0, -signal.SIGTERM):
            reason = stderr.strip() or f"SCK helper exited {proc.returncode}"
            self.last_error = AudioCaptureError(reason)
            self.last_warning = (
                "System audio capture failed — grant Screen Recording in System "
                "Settings → Privacy & Security → Screen Recording, then "
                f"re-record. ({reason})"
            )
            logger.error("SCK helper failed: %s", reason)
        self._proc = None
        self._reader = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_system_audio.py -k sck -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/system_audio.py tests/test_system_audio.py
git commit -m "feat(system-audio): ScreenCaptureKitSystemCapture subprocess driver"
```

---

### Task 6: `select_system_backend()`

**Files:**

- Modify: `src/system_audio.py`
- Test: `tests/test_system_audio.py`

**Interfaces:**

- Consumes: `BlackHoleSystemCapture` (Task 4), `ScreenCaptureKitSystemCapture` (Task 5), `resolve_helper_path` (Task 3), `AudioConfig`, `AudioCaptureError`.
- Produces: `select_system_backend(config: AudioConfig) -> SystemAudioBackend`. Auto rule: `blackhole`/`screencapturekit` force the backend; `auto` → SCK when macOS ≥ 13 **and** `resolve_helper_path()` is not None, else BlackHole. Explicit `screencapturekit` with no helper raises `AudioCaptureError`. Also `_macos_at_least(major: int) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_system_audio.py`:

```python
def test_select_backend_explicit_blackhole(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="blackhole")
    with patch("src.system_audio.resolve_helper_path", return_value=Path("/x/helper")):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)


def test_select_backend_explicit_sck_without_helper_raises(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="screencapturekit")
    with patch("src.system_audio.resolve_helper_path", return_value=None):
        import pytest
        with pytest.raises(AudioCaptureError):
            sa.select_system_backend(cfg)


def test_select_backend_auto_prefers_sck_when_available(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with patch("src.system_audio.resolve_helper_path", return_value=tmp_path / "helper"), \
         patch("src.system_audio._macos_at_least", return_value=True):
        assert isinstance(sa.select_system_backend(cfg), sa.ScreenCaptureKitSystemCapture)


def test_select_backend_auto_falls_back_without_helper(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with patch("src.system_audio.resolve_helper_path", return_value=None), \
         patch("src.system_audio._macos_at_least", return_value=True):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)


def test_select_backend_auto_falls_back_on_old_macos(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with patch("src.system_audio.resolve_helper_path", return_value=tmp_path / "helper"), \
         patch("src.system_audio._macos_at_least", return_value=False):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_system_audio.py -k select_backend -v`
Expected: FAIL — `AttributeError: module 'src.system_audio' has no attribute 'select_system_backend'`.

- [ ] **Step 3: Implement the selector**

In `src/system_audio.py`, add `import platform` and:

```python
def _macos_at_least(major: int) -> bool:
    """True when the current macOS major version is >= major."""
    try:
        version = platform.mac_ver()[0]
        return int(version.split(".")[0]) >= major
    except (ValueError, IndexError):
        return False


def select_system_backend(config: AudioConfig) -> SystemAudioBackend:
    """Choose the system-audio backend from config + host capabilities.

    "auto" prefers ScreenCaptureKit (macOS 13+ and helper bundled), else
    BlackHole. Screen Recording *permission* is handled at runtime by the SCK
    driver, not here — a first-run undetermined grant still takes the SCK path.
    """
    backend = config.system_capture_backend
    if backend == "blackhole":
        return BlackHoleSystemCapture(config)

    helper = resolve_helper_path()
    if backend == "screencapturekit":
        if helper is None:
            raise AudioCaptureError(
                "system_capture_backend=screencapturekit but the SCK helper "
                "binary was not found (run scripts/build_sck_helper.sh or rebuild "
                "the daemon)."
            )
        return ScreenCaptureKitSystemCapture(config, helper)

    # auto
    if _macos_at_least(13) and helper is not None:
        logger.info("System-audio backend: ScreenCaptureKit (auto)")
        return ScreenCaptureKitSystemCapture(config, helper)
    logger.info("System-audio backend: BlackHole (auto fallback)")
    return BlackHoleSystemCapture(config)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_system_audio.py -v`
Expected: PASS (whole module green).

- [ ] **Step 5: Commit**

```bash
git add src/system_audio.py tests/test_system_audio.py
git commit -m "feat(system-audio): select_system_backend() auto-selection rule"
```

---

### Task 7: Wire the backend into `AudioCapture`

**Files:**

- Modify: `src/audio_capture.py` (`__init__`, `_find_default_input_device`, `_record_loop`, `start`, plus removals)
- Test: `tests/test_audio_capture.py`

**Interfaces:**

- Consumes: `select_system_backend` (Task 6), `SystemAudioBackend` (Task 4).
- Produces: `AudioCapture` unchanged public surface (`start`, `stop`, `wait_for_merge`, `last_warning`, `last_error`, `mic_audio_path`, `on_audio_data`, `on_audio_level`, `on_stream_status`, `on_capture_error`, `active_temp_paths`). Internally the _system_ source is delegated to `self._system_backend`; mic capture + `_merge_sources` are unchanged.

> The BlackHole device lookup and `self._blackhole_idx` move out of `AudioCapture` into `BlackHoleSystemCapture` (Task 4). `_find_default_input_device` no longer excludes the BlackHole index (the name-based exclusion in `resolve_default_mic_index` still skips virtual devices). `_find_device` and its `TestAudioCaptureDeviceLookup::test_find_device_*` tests move to `tests/test_system_audio.py` against `BlackHoleSystemCapture._find_blackhole` (do this in Step 3/Step 1 respectively).

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/test_audio_capture.py`, add a test that `_record_loop` drives the selected backend and still merges. Add:

```python
class TestSystemBackendIntegration:
    @pytest.fixture
    def capture(self, tmp_path):
        return AudioCapture(AudioConfig(temp_audio_dir=str(tmp_path), mic_enabled=False))

    def test_record_loop_uses_selected_backend(self, capture, tmp_path):
        fake_backend = MagicMock()
        fake_backend.latest_rms = 0.0
        fake_backend.last_error = None
        fake_backend.last_warning = None

        def fake_start(path):
            # Simulate the backend writing a valid system WAV.
            sf.write(str(path), np.full(16000, 0.2, dtype="float32"), 16000, subtype="PCM_16")

        fake_backend.start.side_effect = fake_start

        with patch("src.audio_capture.select_system_backend", return_value=fake_backend):
            capture.start()
            time.sleep(0.2)
            out = capture.stop(blocking=True)
            capture.wait_for_merge(timeout=10)

        fake_backend.start.assert_called_once()
        fake_backend.stop.assert_called_once()
        assert out is not None and out.exists()

    def test_backend_warning_propagates(self, capture):
        fake_backend = MagicMock()
        fake_backend.latest_rms = 0.0
        fake_backend.last_error = None
        fake_backend.last_warning = "grant Screen Recording"
        fake_backend.start.side_effect = lambda path: sf.write(
            str(path), np.zeros(16000, dtype="float32"), 16000, subtype="PCM_16")
        with patch("src.audio_capture.select_system_backend", return_value=fake_backend):
            capture.start()
            time.sleep(0.1)
            capture.stop(blocking=True)
            capture.wait_for_merge(timeout=10)
        assert capture.last_warning == "grant Screen Recording"
```

Also move the four `TestAudioCaptureDeviceLookup::test_find_device_*` BlackHole cases into `tests/test_system_audio.py` (rewritten against `BlackHoleSystemCapture(cfg)._find_blackhole("BlackHole")` and patching `src.system_audio.sd.query_devices`). Keep the `test_find_default_input_device*` mic cases in `tests/test_audio_capture.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_audio_capture.py::TestSystemBackendIntegration -v`
Expected: FAIL — `AttributeError: <module 'src.audio_capture'> does not have the attribute 'select_system_backend'`.

- [ ] **Step 3: Refactor `AudioCapture` to delegate the system source**

In `src/audio_capture.py`:

(a) Add the lazy backend import inside `start()` (NOT at module top — avoids the import cycle) and construct the backend. Replace the BlackHole resolution block in `start()` (currently lines 559–563, the `refresh_input_devices()` + `self._find_device(...)`) with:

```python
            # Re-scan the device table: PortAudio's snapshot is frozen at
            # process start; without this a long-running daemon never sees
            # devices (or default changes) newer than its launch.
            refresh_input_devices()

            # Select the system-audio backend (SCK / BlackHole). Imported here,
            # not at module top, to avoid a cycle (system_audio imports
            # AudioCaptureError from this module).
            from src.system_audio import select_system_backend
            self._system_backend = select_system_backend(self._config)
```

(b) In `__init__`, replace `self._blackhole_idx: int | None = None` (line 64) with:

```python
        self._system_backend = None  # set in start(); type: SystemAudioBackend | None
```

(c) Delete `_find_device` (lines 109–127) — it moves to `BlackHoleSystemCapture._find_blackhole` (Task 4). In `_find_default_input_device`, change the exclusion (line 150) from `exclude = {self._blackhole_idx} if self._blackhole_idx is not None else set()` to:

```python
        exclude: set[int] = set()
```

(d) Rewrite `_record_loop` (lines 172–348). The mic path, level loop, and merge stay; the system SoundFile + system InputStream + `system_callback` are removed and replaced by the backend. Replace the body with:

```python
    def _record_loop(self) -> None:
        """Background thread: drive the system backend + mic stream, then merge."""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        base = Path(self._config.temp_audio_dir)
        self._system_path = base / f"meeting_{timestamp}_system.wav"
        self._output_path = base / f"meeting_{timestamp}.wav"

        use_mic = self._config.mic_enabled and self._mic_idx is not None
        if use_mic:
            self._mic_path = base / f"meeting_{timestamp}_mic.wav"
            logger.info("Dual-source recording: system backend + mic")
        else:
            self._mic_path = None
            logger.info("Single-source recording: system backend only")

        backend = self._system_backend
        mic_file = None
        mic_stream = None
        latest_mic_rms = [0.0]

        try:
            # Forward live-audio + stream-status through shims that read the
            # current AudioCapture callbacks each call (main.py toggles
            # on_audio_data around the live transcriber).
            backend.on_audio_data = self._forward_audio_data
            backend.on_stream_status = self._forward_stream_status
            backend.start(self._system_path)
            if backend.last_error is not None:
                raise backend.last_error

            if use_mic:
                mic_file = sf.SoundFile(
                    str(self._mic_path), mode="w",
                    samplerate=self._config.sample_rate, channels=1, subtype="PCM_16",
                )

                def mic_callback(indata, frames, time_info, status):
                    if status:
                        logger.warning("Mic audio status: %s", status)
                        if self.on_stream_status is not None:
                            try:
                                self.on_stream_status("mic", str(status))
                            except Exception:
                                pass
                    if self._recording:
                        mono = self._to_mono(indata)
                        mic_file.write(mono)
                        if self.on_audio_level is not None:
                            latest_mic_rms[0] = float(np.sqrt(np.mean(mono**2)))

                mic_info = sd.query_devices(self._mic_idx)
                mic_channels = min(mic_info["max_input_channels"], 2)
                mic_stream = sd.InputStream(
                    device=self._mic_idx, samplerate=self._config.sample_rate,
                    channels=mic_channels, dtype="float32",
                    callback=mic_callback, blocksize=1024,
                )
                mic_stream.start()

            logger.info("Capture running (system backend + %s).",
                        "mic" if use_mic else "no mic")

            while self._recording:
                now = time.monotonic()
                if self.on_audio_level and now - self._last_level_time >= LEVEL_EMIT_INTERVAL:
                    self._last_level_time = now
                    try:
                        self.on_audio_level(backend.latest_rms, latest_mic_rms[0])
                    except Exception:
                        pass
                time.sleep(0.05)

        except Exception as e:
            logger.error("Audio capture failed: %s", e, exc_info=True)
            self._last_error = (
                e if isinstance(e, AudioCaptureError)
                else AudioCaptureError(f"Failed to capture audio: {e}")
            )
            self._output_path = None
            self._recording = False
            if self.on_capture_error is not None:
                try:
                    self.on_capture_error(self._last_error)
                except Exception:
                    logger.exception("on_capture_error callback raised")
            self._merge_complete.set()
            return

        finally:
            try:
                if backend is not None:
                    backend.stop()
            except Exception:
                pass
            if mic_stream is not None:
                try:
                    mic_stream.stop(); mic_stream.close()
                except Exception:
                    pass
            if mic_file is not None:
                try:
                    mic_file.close()
                except Exception:
                    pass
            self._streams_stopped.set()

        # Surface a non-fatal backend warning (e.g. Screen Recording not granted).
        if backend is not None and backend.last_warning and not self._last_warning:
            self._last_warning = backend.last_warning

        self._merge_sources()
        self._merge_complete.set()

    def _forward_audio_data(self, mono: np.ndarray) -> None:
        cb = self.on_audio_data
        if cb is not None:
            try:
                cb(mono)
            except Exception:
                pass

    def _forward_stream_status(self, source: str, status: str) -> None:
        cb = self.on_stream_status
        if cb is not None:
            try:
                cb(source, status)
            except Exception:
                pass
```

- [ ] **Step 4: Run the audio-capture + system-audio suites**

Run: `python3 -m pytest tests/test_audio_capture.py tests/test_system_audio.py -v`
Expected: PASS. (The moved device-lookup tests now live in `test_system_audio.py`; the mic default-device tests remain green in `test_audio_capture.py`.)

- [ ] **Step 5: Run the orchestrator + reprocess suites (integration guard)**

Run: `python3 -m pytest tests/test_orchestrator.py tests/test_pipeline_runner.py -v`
Expected: PASS — confirms `derive_source_paths`, the merge, and diarisation still consume `_system.wav`/`_mic.wav` unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/audio_capture.py tests/test_audio_capture.py tests/test_system_audio.py
git commit -m "refactor(audio): delegate system source to SystemAudioBackend (SCK/BlackHole)"
```

---

### Task 8: Expose Screen Recording status via preflight

**Files:**

- Modify: `src/api/routes/preflight.py`
- Test: `tests/test_api_preflight.py`

**Interfaces:**

- Consumes: `select_system_backend` / `ScreenCaptureKitSystemCapture.preflight` (Tasks 5–6), `AudioConfig`.
- Produces: the `/api/preflight` JSON gains a `screen_recording` key: `"granted"` | `"denied"` | `"unknown"` | `"not_applicable"` (the last when the selected backend is BlackHole, i.e. SCK isn't in use).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_preflight.py` (follow the file's existing client/fixture pattern; if it builds the app via a helper, reuse it):

```python
def test_preflight_includes_screen_recording(monkeypatch):
    import src.api.routes.preflight as pf

    class FakeSCK:
        def preflight(self):
            return "granted"

    monkeypatch.setattr(pf, "select_system_backend", lambda cfg: FakeSCK())
    # run_preflight is unrelated here; leave it returning its normal report.
    report = _call_preflight_sync()  # helper that invokes the route coroutine
    assert report["screen_recording"] == "granted"
```

If `tests/test_api_preflight.py` has no sync helper, add one:

```python
import asyncio
import src.api.routes.preflight as pf

def _call_preflight_sync():
    return asyncio.get_event_loop().run_until_complete(pf.preflight())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_api_preflight.py -k screen_recording -v`
Expected: FAIL — `KeyError: 'screen_recording'`.

- [ ] **Step 3: Add the screen-recording probe to the route**

In `src/api/routes/preflight.py`, extend imports and the handler:

```python
from src.system_audio import ScreenCaptureKitSystemCapture, select_system_backend
```

```python
@router.get("/api/preflight", summary="Pre-flight audio + permission checks")
async def preflight() -> dict[str, Any]:
    try:
        config = load_config()
        audio_config: AudioConfig = config.audio
    except Exception as e:
        logger.warning("Failed to load config for preflight: %s", e)
        audio_config = AudioConfig()

    report = run_preflight(audio_config)
    result = report.to_dict()

    # Report Screen Recording status when SCK is the active system backend.
    screen_recording = "not_applicable"
    try:
        backend = select_system_backend(audio_config)
        if isinstance(backend, ScreenCaptureKitSystemCapture):
            screen_recording = backend.preflight()
    except Exception as e:
        logger.warning("Screen Recording preflight failed: %s", e)
        screen_recording = "unknown"
    result["screen_recording"] = screen_recording
    return result
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_api_preflight.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/preflight.py tests/test_api_preflight.py
git commit -m "feat(preflight): report Screen Recording status when SCK backend is active"
```

---

### Task 9: Build & sign the helper into the daemon bundle

**Files:**

- Modify: `scripts/build_daemon.sh` (compile + inside-out sign the helper into the `.app`)
- Modify: `context-recall.spec` (clarifying comment only)
- Verify (manual/local): rebuild the daemon and confirm the helper is present + signed inside the bundle

**Interfaces:**

- Consumes: `macos/sck-audio-capture/main.swift` (Task 2), the identity-selection logic already in `build_daemon.sh`.
- Produces: `dist/context-recall-daemon/Context Recall Daemon.app/Contents/Resources/sck-audio-capture`, signed with `dev.jamiewhite.contextrecall.sck`, sealed inside the outer app.

- [ ] **Step 1: Insert the helper compile+sign block in `build_daemon.sh`**

In `scripts/build_daemon.sh`, after the `sign_adhoc()` function definition (ends line 102) and **before** the outer-app signing block (`if [ "$SIGN_IDENTITY" = "-" ]` at line 104), insert:

```bash
# --- Compile + inject the ScreenCaptureKit system-audio helper -------------
# The daemon spawns this signed Swift binary to capture system audio via the
# Screen Recording TCC service (works on macOS betas where the Microphone
# service — and thus the BlackHole input — is broken). Sign it FIRST so the
# outer-app seal below covers an already-signed nested binary (inside-out).
HELPER_SRC="macos/sck-audio-capture/main.swift"
HELPER_DEST="$APP_DIR/Contents/Resources/sck-audio-capture"
HELPER_IDENTIFIER="dev.jamiewhite.contextrecall.sck"
if command -v swiftc >/dev/null 2>&1 && [ -f "$HELPER_SRC" ]; then
    echo "==> Compiling SCK audio helper"
    swiftc -O "$HELPER_SRC" -o "$HELPER_DEST"
    if [ "$SIGN_IDENTITY" = "-" ]; then
        codesign --force --sign - --identifier "$HELPER_IDENTIFIER" "$HELPER_DEST"
    else
        codesign --force --sign "$SIGN_IDENTITY" --identifier "$HELPER_IDENTIFIER" \
            --timestamp=none "$HELPER_DEST" 2>/dev/null || \
            codesign --force --sign - --identifier "$HELPER_IDENTIFIER" "$HELPER_DEST"
    fi
    echo "==> SCK helper signed and placed at Contents/Resources/sck-audio-capture"
else
    echo "==> WARNING: swiftc or $HELPER_SRC missing — daemon degrades to BlackHole (no SCK)"
fi
```

- [ ] **Step 2: Add the clarifying spec comment**

In `context-recall.spec`, after the `datas += collect_data_files("sqlite_vec")` block (around line 50), add:

```python
# The ScreenCaptureKit system-audio helper (macos/sck-audio-capture) is NOT
# collected here — it is a separately-compiled Swift binary that build_daemon.sh
# injects into Contents/Resources/ and signs inside-out after PyInstaller runs
# (mirroring the mlx.metallib fixup). Keeping it out of the spec avoids a Swift
# build step inside PyInstaller.
```

- [ ] **Step 3: Verify the build wires the helper in (manual/local)**

Run: `./scripts/build_daemon.sh`
Then:

```bash
APP="dist/context-recall-daemon/Context Recall Daemon.app"
HELPER="$APP/Contents/Resources/sck-audio-capture"
test -x "$HELPER" && echo "helper present"
codesign --verify --verbose=1 "$HELPER" && echo "helper seal OK"
codesign -d -r- "$HELPER" 2>&1 | grep -q "contextrecall.sck" && echo "helper DR OK"
codesign --verify --verbose=1 "$APP" && echo "outer app seal OK (helper sealed inside)"
```

Expected: all four echo lines print. (On a machine without the self-signed cert both the helper and app fall back to ad-hoc — `--verify` still passes.)

- [ ] **Step 4: Commit**

```bash
git add scripts/build_daemon.sh context-recall.spec
git commit -m "build(daemon): compile + inside-out sign the SCK helper into the bundle"
```

---

### Task 10: Full suite + lint gate

**Files:** none (verification only).

- [ ] **Step 1: Run the full Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (~1180 existing + the new `test_system_audio.py` cases; no regressions).

- [ ] **Step 2: Lint**

Run: `ruff check src/ tests/`
Expected: clean (no new findings).

- [ ] **Step 3: Import sanity**

Run: `python3 -c "from src.audio_capture import AudioCapture; from src.system_audio import select_system_backend; print('imports OK')"`
Expected: `imports OK` (confirms no import cycle between `audio_capture` and `system_audio`).

- [ ] **Step 4: Commit (if any lint fixups were needed)**

```bash
git add -A
git commit -m "chore(sck): lint + full-suite green for SCK system-audio backend" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec coverage:**

- Swift helper (capture, `--check-permission`, SIGTERM finalise, `rms=` lines, 16 kHz mono) → Task 2. ✅
- `src/system_audio.py` (`SystemAudioBackend`, `BlackHoleSystemCapture`, `ScreenCaptureKitSystemCapture`, `select_system_backend`, `resolve_helper_path`) → Tasks 3–6. ✅
- `AudioCapture` integration, mic/merge unchanged → Task 7. ✅
- `AudioConfig.system_capture_backend` (auto default + validation) → Task 1. ✅
- Build/sign (`build_daemon.sh`, `build_sck_helper.sh`, spec note) → Tasks 2 & 9. ✅
- Preflight `screen_recording` surface → Task 8. ✅
- Tests (config, selection, SCK driver faked, BlackHole parity, integration, native smoke) → Tasks 1–8, 10. ✅
- Behaviour-on-beta (silent mic → all "Other") — no code; guaranteed by leaving the merge/diariser untouched (verified in Task 7 Step 5). ✅
- Key risk de-risked first (daemon-child attribution spike) → Task 2 Step 7. ✅

**2. Placeholder scan:** No TBD/TODO; every code step has complete code. The only "manual/local" steps (Task 2 smoke/spike, Task 9 build verify) are inherent to native SCK + signing and are marked as such with exact commands.

**3. Type consistency:** `select_system_backend(config) -> SystemAudioBackend` used identically in Tasks 6/7/8. Backend attrs `latest_rms`/`last_error`/`last_warning`/`on_audio_data`/`on_stream_status` and methods `start(output_path)`/`stop()`/`preflight()` match across Tasks 4–8. `HELPER_NAME`/`resolve_helper_path` consistent (Tasks 3, 6). Helper CLI (`--output`, `--sample-rate`, `--check-permission`, `rms=`, `error=`) identical across the Swift source (Task 2), the driver (Task 5), and the test stub (Task 5).

## Notes for the executor

- **Do Task 2 first and confirm the spike (Step 7).** Everything downstream assumes a daemon-spawned helper can hold a Screen Recording grant. If it can't, stop and re-evaluate before writing Tasks 3–9.
- **Import cycle:** `system_audio.py` imports `AudioCaptureError` from `audio_capture.py` at top level; `audio_capture.py` imports `system_audio` **only lazily** inside `start()`. Task 10 Step 3 verifies this.
- **Deferred (documented in the spec, not in this plan):** live-transcription/full level-metering of the SCK source beyond the `rms=` heartbeat; a dedicated Settings "Grant Screen Recording" banner (the `screen_recording` preflight field is the hook for it).
- **On this beta:** SCK captures remote audio; the mic stays silent → the energy diariser labels every segment "Other". "Me" attribution returns automatically once Apple fixes Microphone TCC.

```

```
