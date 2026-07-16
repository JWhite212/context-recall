# Insights + Automations Depth — Design

**Date:** 2026-07-16
**Status:** Approved (design), pending spec review
**Branch:** `feat/insights-automations-depth`

## Background

Context Recall already ships basic versions of Circleback's two signature features
(built 2026-07-14, on `main`):

- **Custom Insights** (`src/insights/`, routes `insights.py`, tables `insight_definitions` /
  `insight_results`, UI `InsightsSection.tsx` / `MeetingInsights.tsx` / `InsightsPanel.tsx`) —
  user-defined LLM extractions. Output today is a **flat list of `{content, speaker}`** text
  snippets.
- **Automations** (`src/automations/`, routes `automations.py`, tables `automation_rules` /
  `automation_dispatches`, UI `AutomationsSection.tsx`) — a conditions→actions rules engine.
  Conditions: `tag`, `client`, `project`, `title_contains`, `attendee_domain` (flat `all`/`any`).
  Actions: `apply_tag`, `webhook`, `notify`. The `webhook` action sends only a bare
  _"Automation X matched"_ body — **not** the meeting's actual content.

A competitive analysis against Circleback (and Granola/Otter/Fireflies/Fathom) found the app is
at parity on transcription, botless capture, cited Ask, templates, action items, and
Notion/Obsidian export. The real gaps are **depth** in these two features. This design closes
that depth gap.

## Goals

1. **Structured (typed) insights** — a definition can extract a structured record with
   user-defined typed fields (e.g. `Go-live date: date`, `Blockers: list`), not only flat text.
2. **Automations that do real work** — a `run_insight` action (run a specific insight per
   meeting-type) and a `send_notes` action (POST the real meeting payload, mirroring Circleback's
   documented webhook schema for drop-in compatibility).
3. **Seeded starter content** tailored to the user's consulting workflow, editable/deletable,
   seeded once and never re-seeded after edits.

## Non-goals (YAGNI)

- Native Slack app / Linear / monday / HubSpot / Salesforce integrations.
- Auto-emailing notes to attendees.
- Insight timestamp provenance (speaker-only is retained).
- Nested condition groups, invitee-count / external-meeting conditions.
- A per-rule "push to Notion" action (the global Notion writer already exports; we render
  insights into it).

## Design principles

- **Extend, don't replace.** Every change is additive and backward-compatible. `output_mode`
  defaults to `'list'` = today's exact behaviour. New automation actions are new `type` values;
  existing rules are untouched.
- **Follow existing patterns.** Guarded/non-fatal post-processing steps, reprocess-safe
  delete-then-insert, pure unit-testable evaluators, Claude/Ollama dual backend, `_safe_add_column`
  migrations.

---

## Part A — Structured (typed) Insights

### Data model (migration v23)

`insight_definitions` — add:

- `output_mode TEXT NOT NULL DEFAULT 'list'` — `'list'` | `'structured'`.
- `fields_json TEXT` (nullable) — array of field descriptors for structured mode.

`insight_results` — add:

- `fields_json TEXT` (nullable) — the structured record for a structured-mode result.

Field descriptor shape:

```json
{ "key": "go_live_date", "label": "Go-live date", "type": "date" }
```

`type ∈ {"text", "number", "date", "boolean", "list"}`. `key` is a stable slug (unique within a
definition); `label` is the display name. `list` = array of short strings.

Migration mirrors `tests/test_db_migration_v20.py`, using the existing `_safe_add_column` helper.
Existing rows get `output_mode='list'`, `fields_json=NULL` → identical current behaviour.

### Extractor (`src/insights/extractor.py`)

- **List mode** (unchanged): returns `[{definition_id, definition_name, content, speaker}]`.
- **Structured mode**: system prompt instructs the LLM to return a single JSON **object** whose
  keys are exactly the defined field `key`s, honouring types (ISO `YYYY-MM-DD` for dates, JSON
  numbers, `true`/`false`, arrays for lists, `null` when absent). Then:
  - Parse (reuse existing fenced-JSON stripping) and **coerce** each value to its declared type;
    invalid/missing → `null`. Never raise — a failed field is `null`, a failed call is skipped
    (as today).
  - Produce **one** result row: `fields_json` = the coerced record; `content` = a human-readable
    rendering (`"Go-live date: 2026-09-02 · Blockers: X; Y · Owner: Jamie"`) so every existing
    consumer of `content` (export, search, UI fallback) keeps working.
  - `speaker` = `""` for structured records (record spans the meeting).
- Dual backend (`_claude_chat` / `_ollama_chat`) reused unchanged.

### Repository (`src/insights/repository.py`)

- `create` / `update` / `_row_to_dict` carry `output_mode` + `fields` (parsed from `fields_json`).
- `results_for_meeting` returns parsed `fields` alongside `content`/`speaker`.
- **New:** `replace_results_for_definition(meeting_id, definition_id, results)` — delete then
  insert scoped to a single definition for that meeting. Required so the `run_insight` automation
  action (which runs _after_ the global insight step) does not clobber other definitions' results.
- `replace_results_for_meeting` (global step) is retained.

### Route (`src/api/routes/insights.py`)

- Accept/validate `output_mode` + `fields` on create/update: known types only, unique non-empty
  keys, `fields` required & non-empty when `output_mode='structured'`. Return them on GET.

### UI

- `InsightsSection.tsx`: List/Structured toggle; when Structured, a field-row editor
  (label + type dropdown; add/remove rows). Auto-derive `key` from `label` (slugify).
- `MeetingInsights.tsx`: render a structured result as a labelled key→value card; list results
  unchanged. Empty/`null` fields shown as "—".
- `lib/types.ts` + `lib/api.ts`: extend `InsightDefinition` (`output_mode`, `fields`) and the
  result type (`fields`).

### Export

- Ensure insights (list + structured) render into the markdown export (`src/output/markdown_writer.py`)
  and Notion page (`src/output/notion_writer.py`). Verify current behaviour first; add a minimal
  "Insights" section if absent. Structured records render as a labelled list.

---

## Part B — Automations depth

Two new action types in `src/automations/executor.py`. No table change (actions are JSON).

### `run_insight`

- Params: `{ "type": "run_insight", "definition_id": "<uuid>" }`.
- Loads the definition, runs `InsightExtractor` on the meeting transcript, stores via
  `replace_results_for_definition`. Mirrors Circleback's "insights are automation actions" model —
  run "Client Call Details" only when the meeting looks like a client call.
- Idempotent → runs **regardless** of `run_side_effects` (like `apply_tag`), so reprocess
  regenerates it. LLM call executed off the event loop (thread), consistent with other blocking
  LLM calls in post-processing.
- Requires the executor to reach: the meeting transcript (reconstructed from the **stored**
  `meeting.transcript_json` — already fetched by `_run_automations`, so it works identically on
  fresh and reprocess paths), `InsightRepository`, and an `InsightExtractor` (built from
  `SummarisationConfig`). Supplied via a small services bundle (see Wiring).

### `send_notes`

- Params: `{ "type": "send_notes", "url": "...", "include_transcript": false, "secret": "" }`.
- Builds the **Circleback-schema** payload and POSTs it. Pure builder
  `build_circleback_payload(meeting, action_items, insights, *, include_transcript)` (unit-tested):

  ```json
  {
    "id": "<meeting id>",
    "name": "<title>",
    "createdAt": "<ISO8601>",
    "duration": <seconds>,
    "url": null,
    "tags": ["..."],
    "attendees": [{ "name": "...", "email": "..." }],
    "notes": "<markdown summary>",
    "actionItems": [{ "id", "title", "description", "assignee": {"name","email"}|null, "status": "PENDING|DONE" }],
    "transcript": [{ "speaker", "text", "timestamp" }],   // only if include_transcript
    "insights": { "<definition name>": [ { "insight": <string|object>, "speaker": <string|null> } ] }
  }
  ```

  - `actionItems[].status` mapped to Circleback's `PENDING`/`DONE` (our `open`→`PENDING`,
    `completed`→`DONE`; `cancelled` omitted).
  - `insights[name]`: list-mode → array of `{insight: content, speaker}`; structured-mode →
    array with one `{insight: <fields object>, speaker: null}`.
  - When `secret` set, sign the **raw request body** with HMAC-SHA256 and send as the
    `x-signature` header (mirrors Circleback verification).

- Gated under `run_side_effects` (won't re-fire on reprocess).
- Reads the summary/notes from the stored `meeting` record; action items via the action-items
  repo; insights via `InsightRepository` (all by `meeting_id`) — supplied through the services
  bundle. No live transcript threading needed.

### Conditions

Unchanged. `title_contains` / `tag` / `client` / `project` / `attendee_domain`, `all`/`any`.

### Wiring (`src/pipeline_runner.py` `_run_automations`)

- Construct a services bundle `{ insight_repo, action_items_repo, summarisation_config }` and
  pass it (plus the already-fetched `meeting`) to `ActionExecutor`. `run_insight` rebuilds a
  `Transcript` from `meeting.transcript_json`; `send_notes` reads notes/action-items/insights by
  `meeting_id`. Constructing the bundle is cheap and lazy — rules with no new action types pay
  nothing.
- Order preserved: automations remain step 6 (after the global insight step 5), so a
  `run_insight` action's scoped replace lands cleanly.

### UI (`AutomationsSection.tsx`)

- Action editor gains **Run insight** (definition dropdown) and **Send notes to webhook**
  (URL + optional signing secret + include-transcript toggle). Existing `apply_tag` / `webhook` /
  `notify` editors unchanged.

---

## Part C — Seeded starter content

Idempotent boot routine (`src/insights/seed.py` + automation seeding), gated by a settings marker
`insights_seed_version`. Seeds once; after the user edits/deletes, it never re-seeds. Seeds into
whichever profile DB the daemon opens (dev/prod separated by design).

**Rules trigger on `title_contains`, not tags** — Context Recall generates its own tags and will
not carry the user's Circleback `Type/…` taxonomy, so title matching is the robust trigger.

### Seeded structured insights

| Name                    | Fields                                                                                                   |
| ----------------------- | -------------------------------------------------------------------------------------------------------- |
| **Client Call Details** | Go-live date (date), Blockers (list), Risks (list), Decisions (list), Owner / next step (text)           |
| **Standup Snapshot**    | Per-project status (list), Overdue task count (number), Absences & coverage (list), Key deadlines (list) |
| **Discovery Notes**     | Requirements (list), Open questions (list), Scope decisions (list), Compliance / PCI flags (text)        |

### Seeded automation rules

Seed definitions first, capture their IDs, then create rules referencing them (one idempotent
routine):

- title contains `UAT` / `client` / `review` → `run_insight: Client Call Details`
- title contains `catchup` / `standup` → `run_insight: Standup Snapshot`
- title contains `discovery` → `run_insight: Discovery Notes`

(Rules seeded **disabled by default is not required** — they are safe to run and produce value;
seed them **enabled**. `run_insight` only calls the LLM when a rule matches.)

---

## Testing (TDD)

Python (`pytest`):

- **Extractor** — structured parse + type coercion (date/number/boolean/list), malformed JSON,
  empty transcript, missing field → `null`, human-readable `content` rendering.
- **Repository** — `fields_json` round-trip; `replace_results_for_definition` does not touch other
  definitions' rows; list-mode rows still work.
- **Migration v23** — mirrors `test_db_migration_v20.py`; old rows read back as `list` mode.
- **Executor** — `run_insight` stores scoped results and runs under `run_side_effects=False`;
  `send_notes` gated by `run_side_effects`; HMAC signature correctness.
- **Payload builder** — pure test asserting Circleback-schema field names, status mapping, and
  list-vs-structured `insights` shape.
- **Seed** — idempotent (second call no-ops); does not re-create after deletion (marker honoured).
- **Route** — validation of `output_mode`/`fields`.

UI (`vitest`):

- `InsightsSection` field editor (add/remove, slugify key, structured toggle).
- `MeetingInsights` structured card rendering.
- `AutomationsSection` new action editors.

Full suites green: `python3 -m pytest tests/ -v`, `cd ui && npm test`, `ruff check src/ tests/`,
`npx tsc --noEmit`.

## Migration & rollout

- Single migration bump `SCHEMA_VERSION = 23`.
- Additive columns only; no data backfill needed (defaults preserve behaviour).
- Seed runs at daemon boot; safe on existing prod DBs (marker-gated).

## Risks & mitigations

- **`run_insight` clobbering global results** → scoped `replace_results_for_definition`.
- **LLM returns non-conforming JSON for structured fields** → per-field coercion to `null`, never
  raise; definition-level failure skipped (existing pattern).
- **Seed re-running / clobbering user edits** → `insights_seed_version` marker.
- **Seeded rules never matching** (tag taxonomy mismatch) → title-based triggers.
- **Webhook payload leaking transcript** → `include_transcript` defaults `false`.
