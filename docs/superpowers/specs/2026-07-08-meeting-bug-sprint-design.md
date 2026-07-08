# Meeting Bug-Fix Sprint — Design

**Date:** 2026-07-08
**Branch:** `fix/meeting-bug-sprint` (off `origin/main` @ `1d19851`)
**Scope:** Five user-reported bugs. Calendar import and competitor research are explicitly **out of scope** for this spec (tracked as separate follow-on work).

## Goal

Fix the five bugs the user hits in daily use, each as an isolated, test-driven change with its own commit. Where a report is really a design problem (fragmented labeling) or an operational problem (system audio), fix the underlying cause rather than papering over the symptom.

## Working method

- Strict TDD: failing test first, then implementation, per bug.
- One fix per commit; a bug that spans backend + UI may be one commit if the halves are meaningless apart, otherwise split.
- Backend suite (`python3 -m pytest tests/ -q`) and, for UI-touching bugs, `cd ui && npm test` + `npx tsc --noEmit` green before each commit.
- CodeRabbit review pass before the sprint is declared done.

## Sequencing

1. **#2/#1 — Unified editable tags** (data-model + UI; unblocks the biggest daily friction)
2. **#4 — Speaker→person assignment** (debug-first, then fix)
3. **#3 — "Thank you" silence hallucination** (filter enhancement)
4. **#5 — System-audio watchdog** (diagnose build first; add watchdog regardless)

---

## Bug #1 + #2 — Unify tags & label into one editable multi-tag control

### Problem

The meeting header renders four competing labeling concepts:

- **`tags`** (`meetings.tags`, JSON array) — LLM-generated topic tags parsed from the summary's `## Tags` section (`src/summariser.py:144-155`). Displayed **read-only** as pills (`ui/src/components/meetings/MeetingDetail.tsx:667-678`). No remove, no edit. → _"auto-tags can't be removed/changed."_
- **`label`** (`meetings.label`, single string) — manual, committed on blur/Enter via `LabelEditor` (`MeetingDetail.tsx:304-412`). Single value only. → _"no apply button; can't apply more than one."_
- **client / project** — structured assignment via `AssignmentSelect`; works, stays as-is.

The meeting list already filters on the **`tags`** array (`MeetingList.tsx:41,54,63`), so tags — not `label` — are the real discovery axis.

### Design

Make **`tags`** the single, user-editable, multi-value labeling system. The LLM still seeds tags at summarisation time, but every tag is removable and the user can add unlimited tags. Fold the legacy single `label` into `tags`. Client/project remain a separate structured concept.

### Backend

- **Repository** (`src/db/repository.py`): `tags` is already mutable (`_MUTABLE` line 32; serialized at 228-229). Add `get_distinct_tags()` (union of all tags across meetings, sorted) mirroring the existing `get_distinct_labels()` (429).
- **New route** `PATCH /api/meetings/{id}/tags` (`src/api/routes/meetings.py`): body `{ "tags": ["a","b"] }`, **replace** semantics, validated (non-empty strings, trimmed, de-duped, sane length/count caps). Returns the updated meeting.
- **New route** `GET /api/meetings/tags` for autocomplete suggestions (parallel to existing `GET /api/meetings/labels` at line 135).
- **Migration** (new `SCHEMA_VERSION`, `src/db/database.py`): for every meeting with a non-empty `label`, append that value to `tags` (dedupe) so no user data is lost. The `label` column is retained for back-compat but no longer surfaced or written by the UI. Existing `PATCH /label` and `GET /labels` endpoints stay (unused by UI) to avoid breaking any external caller.

### UI

- New `TagEditor` component (replaces the read-only pills at 667-678 **and** the `LabelEditor` block at 680-683 in `MeetingDetail.tsx`): renders each tag as a chip with an `×` remove control, plus a text input that commits on Enter or an explicit **Add** button, with autocomplete sourced from `GET /api/meetings/tags`. All mutations call the new `PATCH .../tags`.
- `api.ts`: add `setMeetingTags(id, tags)` and `getTags()`; retain `setMeetingLabel`/`getLabels` until the `LabelEditor` is fully removed.
- Optimistic update + toast on success/failure; show pending state on the control.
- `MeetingList` tag filter is unchanged (still reads the `tags` array).

### Tests

- Repo: PATCH replaces tags; dedupe/trim; `get_distinct_tags` union; migration folds `label` into `tags`.
- API: `PATCH /tags` happy path + validation (empty, over-cap, non-string).
- Migration test (`tests/test_db_migration_v<next>.py`) following the existing numbered pattern.
- UI (vitest): add tag (Enter + button), remove tag, autocomplete render, mutation error toast.

---

## Bug #4 — Speaker→person assignment fails ("failed to apply to person")

### Problem

`AssignSpeakerMenu.tsx:59` (`onError: () => toast.error("Failed to assign person")`) is a **catch-all** that discards the real backend error, so the true failure is invisible. The endpoint is `POST /api/meetings/{id}/speakers/{sid}/assign-person` (`src/api/routes/people.py:133`), which:

1. Rejects `speaker_id` not matching `_SPEAKER_ID_RE = ^[a-zA-Z0-9_ -]+$` → **422** (rejects labels with `.`, `'`, `(`, `,`, or accented characters — e.g. a speaker already renamed to `John O'Brien` or `María`).
2. Imports `from src.voice.enrolment import extract_speaker_windows` at call time (line 157) and runs enrolment (best-effort) — a potential **500** if an import/DB path throws.

### Approach — debug first (systematic-debugging), then fix

Pin the exact status/detail the running daemon returns for the failing case before committing a fix. Likely fixes (confirm which apply):

- **Surface the real error** in the UI: pass through `error.detail`/status into the toast for both `assign` and `createAndAssign` mutations (`AssignSpeakerMenu.tsx:59,73`).
- **Relax `_SPEAKER_ID_RE`** to accept the labels the app actually produces and renames to (Unicode letters, apostrophes, parentheses, commas, periods) — or match against the _actual_ speaker labels present in the transcript rather than a character allowlist.
- **Decouple rename from enrolment**: the speaker rename (`set_speaker_name`) must succeed and persist even when voice enrolment can't run; enrolment failure already returns a reason but must never turn the whole request into a non-2xx.

### Tests

- API: `assign-person` succeeds for labels containing apostrophes/accents/parentheses (currently 422).
- API: rename persists when enrolment is unavailable (speechbrain absent) — response is 2xx with `enrolled=false` + reason.
- UI: error toast shows the backend detail, not a generic string.

---

## Bug #3 — "Thank you" hallucinated repeatedly during silence

### Problem

`src/transcriber.py` already suppresses hallucinations (`no_speech_threshold`, `hallucination_silence_threshold`, `compression_ratio_threshold`, and a repetition filter at 99-112), but the repetition filter only trips on **5+ identical consecutive words** — a repeated 2-word phrase ("thank you", "thanks for watching") slips through. The batch path has no per-segment silence gate (the live path has an RMS gate at `live_transcriber.py:142-145`; the batch path does not use it).

### Design

- Add **phrase-level** repetition detection: drop/flag a segment whose text is a short phrase repeated back-to-back (repeated n-gram ≥ N times), not just single words.
- Add a small **known-hallucination phrase** suppression set (Whisper's canonical silence artifacts: "thank you", "thanks for watching", "you", punctuation-only) applied when the segment coincides with a low-energy / high-no-speech window.
- Apply the same logic to **both** `transcriber.py` (batch) and `live_transcriber.py` (live dedup at 288-308) so the UI and the stored transcript agree.
- Dropped segments continue to flow into `transcript.dropped_segments` (already surfaced in the UI at `MeetingDetail.tsx:209-214`) — nothing is silently discarded.
- Thresholds/phrase-list configurable under the `transcription` config section.

### Tests

- Unit: synthetic segment lists where "thank you" repeats over silence are dropped; legitimate repeated phrasing in real speech is retained.
- Unit: the known-phrase set only fires on low-energy/no-speech windows, not on genuine mid-meeting "thank you".

---

## Bug #5 — System / remote audio not recorded (mic only)

### Problem

Dual-source capture (BlackHole system + mic, merged post-recording) exists (`src/audio_capture.py`). Mic works, system doesn't ⇒ **BlackHole is not capturing** — the default output isn't routed through BlackHole, or BlackHole is silent. Recent deploys fixed the **mic** side (TCC grant + auto-routing), not this failure mode. There is no error when the system stream stays at zero, so the result is a silently mic-only recording.

### Approach

1. **Diagnose operational vs code** (needs the user's environment): current daemon build, whether BlackHole is installed, and whether system output is routed through the managed "Context Recall Audio" multi-output device. If it's routing/config, the fix is operational (rebuild / re-route) with no code change.
2. **Code improvement regardless — system-source silence watchdog.** Extend the existing per-source RMS watchdog (`src/silent_input_detector.py`, A1 fix) so that if the **system** source RMS stays at ~0 for N seconds during an active capture, the orchestrator emits a `pipeline.warning` ("system audio not being captured — check output routing / BlackHole") instead of producing a mic-only recording with no signal. Verify `AudioRouter` (`src/audio_routing.py`) actually engaged the multi-output device at capture start and warn if not.

### Tests

- Unit: watchdog emits a warning when the system source is silent past the threshold; stays quiet when audio flows.
- Unit: router pre-flight warns when the default output does not feed BlackHole at capture start.

_(If diagnosis shows the user's issue is purely operational, the code change still lands as a guardrail so this fails loudly next time.)_

---

## Out of scope (separate follow-on specs)

- **Calendar import / calendar-as-source** (#6) — net-new feature; own spec.
- **Circleback + market competitor research and gap features** (Track C) — research deliverable + backlog; own spec.

## Risks & notes

- The tags migration must be idempotent and preserve existing tags + folded labels; covered by a dedicated migration test.
- Bug #4's fix is gated on reproducing the exact failure; the spec commits to _surfacing_ the real error first so the root cause is unambiguous.
- Bug #5 may need no code fix; the watchdog is a guardrail either way and is safe to land.
