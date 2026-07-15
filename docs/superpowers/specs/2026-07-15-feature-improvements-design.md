# Feature Improvements Design — Calendar, Rename, Diarisation, Action Items

**Date:** 2026-07-15
**Status:** Approved design; per-feature implementation plans to follow.
**Author:** Brainstorming session (Context Recall)

## Overview

Context Recall's core pipeline works. Four adjacent features are broken or
incomplete and are addressed here as **four independent sub-projects**, each with
its own implementation plan, spec-linked below. Build order is fixed:

1. **Calendar sync** — repair + selection picker (foundational; feeds #2 and #3).
2. **Meeting rename** — manual rename + calendar auto-title.
3. **Multi-speaker diarisation** — neural backend + speaker naming/correction.
4. **Action items** — client/project tags, editing, grouped/filtered views.

Market research informing these designs is captured in the appendix.

### Decisions locked during brainstorming

- **Diarisation:** accuracy-first — proper neural diariser (pyannote) on the
  remote channel; a few extra minutes of post-processing per meeting is acceptable.
- **Rename propagation:** a rename updates the Obsidian note and the Notion page,
  not just the DB/UI.
- **Action-item tagging:** items inherit their meeting's client/project and are
  per-item editable (override).
- **Calendar scope:** fix sync + polish the per-calendar include/exclude picker +
  wire calendar matching. Fireflies-style keyword/domain _recording rules_ are
  explicitly deferred to a later add-on.

---

## Feature 1 — Calendar sync (repair + picker)

### Problem

The Calendar screen is empty, no macOS Calendars permission prompt ever appears,
and recordings never link to calendar events. The feature is ~80% built (EventKit
reader, periodic sync job, mirror table, Settings picker with include/exclude and
"sync now") but dead due to three stacked, independently-confirmed bugs.

### Root causes (confirmed)

1. **Master gate off.** Live prod `config.yaml` has `calendar.enabled: false`.
   The default is also `false` in `src/utils/config.py:CalendarConfig`.
2. **EventKit not bundled.** The deployed daemon `.app` contains no
   `pyobjc-framework-eventkit`. `context-recall.spec` never collects it, so
   `CalendarReader` and `CalendarMatcher` silently no-op in every deployed build
   (same failure class as the earlier speechbrain/voice-ID gap). `requirements.lock`
   _does_ pin `pyobjc-framework-eventkit==12.1`, so this is purely a packaging gap.
3. **Missing TCC usage key.** The daemon `Info.plist` declares
   `NSMicrophoneUsageDescription` but no Calendars usage key. macOS TCC **kills** a
   process that requests a permission it hasn't declared (documented in
   `build_daemon.sh`), so even a correctly-bundled daemon would crash on first
   calendar access rather than prompt.

### Design

- **Bundle EventKit:** add `pyobjc-framework-eventkit` (and its `pyobjc-core` /
  `pyobjc-framework-cocoa` deps) to the PyInstaller spec via `collect_submodules` /
  hidden imports; add a bundle-contents assertion test analogous to the speechbrain
  guard so a future build can't silently drop it again.
- **Declare Calendars TCC keys:** add `NSCalendarsUsageDescription` and
  `NSCalendarsFullAccessUsageDescription` (macOS 14+) to **both** the
  `build_daemon.sh` plist block and the `context-recall.spec` `info_plist` dict.
- **Explicit access request:** add `ensure_calendar_access()` on the boot path,
  mirroring `mic_permission.py` — request EventKit access deliberately so the OS
  prompt fires instead of relying on implicit access from a bare launchd binary.
- **Flip default `enabled=true`** in `CalendarConfig`, and set it in the deployed
  config on next local deploy.
- **Selection UX:** keep the existing include/exclude picker (`CalendarsSection.tsx`).
  Add: (a) a permission-state banner distinguishing "access not granted → Open
  System Settings" from "granted"; (b) an empty-state that distinguishes
  "no permission" from "no events in window". No new selection model — the current
  `excluded_calendars` list stays.
- **Matching** (`calendar_matcher.py`) is unchanged; it is the input to Feature 2
  (auto-title) and the existing auto-arm controller.

### Data flow

macOS Calendar (EventKit) → `CalendarReader.events_in_range()` (excluded filter) →
`CalendarSyncJob.apply()` upsert/prune into the mirror table → Calendar UI + auto-arm.
At recording start, `CalendarMatcher.match(started_at)` resolves the active event and
stores `calendar_event_title` + attendee candidate labels on the meeting row.

### Error handling

- EventKit unavailable (CI, missing framework) → reader returns empty, no crash
  (existing guard preserved).
- Access denied → surfaced as a UI banner + a one-time boot log warning; sync is a
  no-op rather than an error loop.
- Beta-OS TCC risk (macOS 26.6 currently refuses mic prompts) → the calendar prompt
  may be affected too; the permission banner + "Open System Settings" deep-link is
  the fallback path so the feature degrades visibly rather than silently.

### Testing

- Existing fake-backed reader/sync tests retained.
- New: plist contains both Calendars usage keys (assert on generated Info.plist).
- New: bundled daemon includes EventKit (bundle-contents check).
- New: permission-state → UI banner mapping.

---

## Feature 2 — Meeting rename + calendar auto-title

### Problem

Meetings cannot be renamed during or after recording, so the meeting list is full
of ambiguous entries. The `calendar_event_title` column exists but is not applied
as the meeting title.

### Design

- **API:** `PATCH /api/meetings/{id}` accepting `{title}`.
- **Auto-title:** on calendar match, set the meeting title from
  `calendar_event_title`. Track a `title_source` column (`auto` | `manual`).
  Auto-title only applies when `title_source != 'manual'` — a manual rename is
  never clobbered by a later auto-title pass or reprocess.
- **Live rename (during recording):** editable title in the live view →
  REST PATCH → updates the in-flight meeting row and emits a `meeting.renamed`
  WebSocket event so all clients update.
- **Post-hoc rename:** inline edit in the meetings list row and in the detail
  header.
- **Propagation (chosen: update outputs too):**
  - Obsidian: rename the `.md` file to the new title slug and update the frontmatter
    `title`. Reuse `markdown_writer`'s slug logic and its vault-escape guard; guard
    against filename collisions (append a disambiguator or keep the old name on clash).
  - Notion: PATCH the page title via the existing `NotionWriter` (which already
    writes/updates the title property and has archive logic). Keyed on
    `meetings.notion_page_id`.
  - Propagation failures are non-fatal and logged; the DB rename always succeeds.

### Data flow

Rename (UI or auto) → `PATCH /api/meetings/{id}` → repo updates `title` +
`title_source` → emit `meeting.renamed` → propagate to markdown + Notion (best-effort).

### Testing

- `title_source` precedence: auto never overwrites manual; manual survives reprocess.
- Propagation to markdown (fake filesystem: file renamed, frontmatter updated,
  collision handled, vault-escape rejected).
- Propagation to Notion (fake client: title PATCH issued for the stored page id).
- Live-rename emits `meeting.renamed`.

---

## Feature 3 — Multi-speaker diarisation (neural + naming)

### Problem

Diarisation labels everyone who isn't "Me" as a single "Remote" speaker. Real
meetings have multiple remote participants, so "Remote" collapses distinct people
and the transcript attribution is wrong.

### Design

- **Neural backend on the remote channel only.** The mic channel is unambiguously
  "Me"; run pyannote over the _remote/system_ source WAV to separate
  `SPEAKER_1..N` among remote participants. Select via `diarisation.backend`
  (`energy` | `pyannote`), defaulting to `pyannote` (accuracy-first). Keep `energy`
  as the fallback when torch/pyannote is unavailable.
- **Bundle/deploy:** `pyannote_diariser.py` already defers its import; ensure its
  runtime deps are collected in the spec and the diarisation model
  ships/downloads. Degrade to `energy` if the model is absent.
- **Naming via voice-ID:** ECAPA (`src/voice/`) matches `SPEAKER_n` against stored
  voice profiles to name recurring colleagues across meetings.
- **Calendar-attendee seeding:** in small meetings, seed unresolved speaker names
  from calendar attendee candidates (already stored at match time) — Krisp's
  small-meeting heuristic.
- **Correction UX (Alter pattern):** a speaker panel in meeting detail lists
  detected speakers; the user can play back each speaker's segments, and
  reassign/rename a speaker once. The change propagates across the transcript and
  persists in the existing speaker-mapping table (person-linked), so reprocess and
  future voice-ID reuse it.

### Data flow

Remote WAV → pyannote → `SPEAKER_1..N` segments → merge with mic ("Me") →
voice-ID naming + attendee seeding → transcript with per-speaker labels →
user correction → speaker-mapping persistence → re-applied on reprocess.

### Error handling

- pyannote/torch/model unavailable → log + degrade to `energy` (binary Me/Remote).
  Never hard-fail the pipeline.
- Voice-ID unavailable (no speechbrain) → speakers stay `SPEAKER_n` until named
  manually.

### Testing

- Backend selection honours config and degrades when deps absent.
- Multi-speaker label mapping over ≥3 remote speakers.
- Voice-ID naming across speakers; attendee-seeding heuristic in a 2–3 person meeting.
- Manual reassignment persists and survives reprocess.

### Notes

Largest sub-project. Competitively, this puts Context Recall ahead of Granola
(binary Me/Them on desktop) and Meetily (no diarisation) while staying fully local.

---

## Feature 4 — Action items: tags, editing, filtered views

### Problem

Action items cannot be tagged by client/project or grouped/filtered, so they can't
be organised across meetings.

### Design

- **Schema migration:** add nullable `client_id`, `project_id` (FK to
  clients/projects, `ON DELETE SET NULL`) to `action_items`, plus indexes
  (`client_id`, `project_id`).
- **Inheritance + override:** on extraction, each item inherits its source meeting's
  `client_id`/`project_id`. `PATCH /api/action-items/{id}` (already exists) is
  extended to accept `client_id`/`project_id` for per-item override. Reprocess's
  action-item replace must **not** wipe a manual tag override (mirror the
  trackers `replace_hits_for_meeting` reprocess-safety pattern — preserve
  user-set fields).
- **Views:** the Action Items screen gains group-by (project | client | status |
  due | meeting) and filters (client, project, status, priority, due-range,
  assignee). All server-side via query params on `GET /api/action-items`
  (`list_action_items` already accepts filters; extend it) so large lists stay fast.

### Data flow

Extraction → item created with inherited client/project → optional per-item PATCH
override → list endpoint applies group-by + filters → screen renders grouped/filtered.

### Testing

- Inheritance on extract.
- Per-item override persists and survives reprocess (manual tag not wiped).
- Grouped/filtered query correctness (each filter + combinations).

---

## Cross-cutting notes

- **Interdependencies:** Calendar (1) feeds auto-title (2) and attendee-seeding (3).
  Diarisation (3) feeds per-speaker attribution used by action-item assignees (4).
  Hence the build order.
- **Migrations:** Features 2 (`title_source`) and 4 (`action_items.client_id`,
  `project_id`) each add a numbered migration bumping `SCHEMA_VERSION`.
- **Packaging discipline:** Features 1 and 3 both hinge on PyInstaller bundling
  (EventKit; pyannote/model). Each adds a bundle-contents guard test so a deployed
  build can't silently ship without the dependency — the recurring failure mode in
  this project.

---

## Appendix — Market research (2026-07-15)

Deep research over AI meeting-notetaker competitors. Claims below are drawn from
primary vendor documentation (verification panel was rate-limited, but sources are
first-party feature docs, not contested facts).

### Capture & privacy landscape

| Product            | Capture              | Transcription                             | Desktop diarisation                                    |
| ------------------ | -------------------- | ----------------------------------------- | ------------------------------------------------------ |
| Granola            | system+mic, no bot   | cloud (streamed to 3rd party)             | binary Me/Them; multi-speaker only on iPhone in-person |
| Krisp              | no bot, app-agnostic | mostly cloud (local only in limited mode) | Speaker 1/2 → names from calendar contacts, 1:1 only   |
| Alter              | no bot, system audio | on-device (Parakeet V3)                   | on-device, 2–8 participants, with correction UX        |
| Otter / Fireflies  | bot joins call       | cloud                                     | cloud                                                  |
| Meetily (OSS)      | system+mic, no bot   | local                                     | none yet (roadmap)                                     |
| **Context Recall** | system+mic, no bot   | **local (MLX)**                           | energy Me/Remote + ECAPA (→ neural, this spec)         |

### Per-area patterns

- **Calendar:** Granola and Otter support **only Google + Microsoft** directly;
  neither supports Apple Calendar. Context Recall via macOS EventKit gets _every_
  provider (Outlook/iCloud/Google all subscribe into Apple Calendar) through one
  integration — a structural advantage. Selection pattern: sync primary by default,
  toggle others. Fine-tuning frontier (Fireflies): title-keyword and attendee-domain
  recording rules evaluated pre-meeting, per-meeting overrides beating the global —
  deferred here.
- **Rename/auto-title:** Krisp matches recording→event by time and auto-names from
  the event title, editable. Directly matches Feature 2.
- **Diarisation naming:** Alter's correction UX (filter by speaker, play their
  segments, reassign once, propagate) is the gold standard — adopted in Feature 3.
  Krisp seeds names from calendar attendees in small meetings — adopted.
- **Action items:** competitors differentiate on grouping and task-manager sync
  (Asana/Todoist/Notion). Context Recall's existing client/project model makes
  per-item tags + filtered views (Feature 4) mostly plumbing.

### Ranked adaptable ideas

1. Apple-Calendar-as-universal-bridge — lean into it in UX and positioning.
2. Fireflies-style keyword/domain recording rules for auto-arm (future add-on).
3. Alter's speaker-correction panel (Feature 3).
4. Calendar-attendee seeding of speaker names (Feature 3).
5. Recurring-meeting grouping by event-id + title (Granola) — series detection exists.

### Sources

- https://github.com/Zackriya-Solutions/meetily
- https://alterhq.com/meetings
- https://docs.granola.ai/help-center/taking-notes/transcription
- https://docs.granola.ai/help-center/getting-started/syncing-your-calendars
- https://help.krisp.ai/hc/en-us/articles/8326933081116-AI-Meeting-Assistant-FAQ
- https://guide.fireflies.ai/articles/3115936908-how-to-use-recording-rules-to-record-or-skip-specific-meetings
- https://help.otter.ai/hc/en-us/articles/13674910923671-Automatically-add-Otter-Notetaker-to-your-meetings
- https://circleback.ai/blog/best-ai-meeting-assistants
- https://www.spinach.ai/blog/best-ai-tools-automated-action-item-tracking
- https://openwhispr.com/blog/local-speaker-diarization
- https://www.useluminix.com/reports/industry-analysis/ai-meeting-notes-comparison-granola-vs-otter-vs-fireflies-vs-fathom-2026
