# Custom Insights — Design

**Date:** 2026-07-09
**Branch:** `feat/competitor-features` (off `origin/main`; already carries Feature 1 at schema v15)
**Cluster:** Feature 2 of 3 in the intelligence + workflow layer (templates → **custom insights** → automations). This spec covers custom insights only.

## Goal

Let the user define named **insights** (e.g. "Risks", "Decisions", "Objections", "Next steps") that an LLM extracts from every meeting as a **list of short items**, shown on the meeting and re-run safely on reprocess. This is Circleback's "insights" concept, scoped to list-of-items results (structured-field records are a deliberate later enhancement).

Naming: the existing `meeting_insights` route/component is analytics (talk-time, trackers, email). This feature is **custom insights** — user-defined LLM extractions — kept separate to avoid collision.

## Background (patterns reused)

- **Trackers** (`src/trackers/`): `TrackerRepository` has definition CRUD (`create/update/get/list_trackers/delete`) + per-meeting results with `replace_hits_for_meeting` / `hits_for_meeting` — the exact "user-defined definition → per-meeting results, reprocess-safe" shape. Tables `TRACKERS_SQL` / `TRACKER_HITS_SQL` in `src/db/database.py`.
- **Action items** (`src/action_items/`): `ActionItemExtractor(summarisation_config, config).extract(transcript) -> list[dict]` — one blocking LLM call via `summariser._claude_chat`/`_ollama_chat`, fenced-JSON `parse_response`; repository has `delete_extracted_for_meeting` for reprocess.
- **Post-processing** (`src/pipeline_runner.py:770` `_post_process_async`): each stage (`_extract_action_items`, `_scan_trackers`, …) is gated by config, wrapped in try/except (non-fatal), and receives `is_reprocess`. New stages plug in here.
- **Head schema version on this branch is 15** (Feature 1) → new tables land at **v16**.

## Architecture

### Data model (migration v16)

Two tables, mirroring trackers:

- `insight_definitions`: `id TEXT PK`, `name TEXT NOT NULL`, `prompt TEXT NOT NULL` (what to extract), `enabled INTEGER NOT NULL DEFAULT 1`, `created_at REAL NOT NULL`.
- `insight_results`: `id TEXT PK`, `meeting_id TEXT NOT NULL`, `definition_id TEXT NOT NULL`, `definition_name TEXT NOT NULL` (denormalised so results survive a definition rename/delete), `content TEXT NOT NULL`, `speaker TEXT DEFAULT ''`, `created_at REAL NOT NULL`. Indexes on `meeting_id` and `definition_id`.

Added to `SCHEMA_SQL` (fresh install) **and** a new `if current_version < 16:` migration block ending with a literal `PRAGMA user_version = 16` (per the v5–v15 pattern — literal, not `{SCHEMA_VERSION}`).

### Components

**1. `InsightRepository`** (`src/insights/repository.py`, mirrors `TrackerRepository`):

- Definitions: `create(name, prompt, enabled=True) -> str`, `update(id, **fields)`, `get(id) -> dict | None`, `list_definitions(enabled_only=False) -> list[dict]`, `delete(id) -> bool`.
- Results: `replace_results_for_meeting(meeting_id, results: list[dict]) -> int` (delete-then-insert in one transaction), `results_for_meeting(meeting_id) -> list[dict]` (joined/grouped by definition).

**2. `InsightExtractor`** (`src/insights/extractor.py`, mirrors `ActionItemExtractor`):

- `__init__(summarisation_config)` builds a `Summariser`.
- `extract(transcript, definitions: list[dict]) -> list[dict]`: for each enabled definition, one LLM call (`INSIGHT_PROMPT` + the definition's prompt + a bounded transcript excerpt) returning a JSON array of `{ "content": str, "speaker": str | null }`; parsed with the same fenced-JSON tolerance as `ActionItemExtractor.parse_response`. Returns a flat list of `{definition_id, definition_name, content, speaker}`. Any per-definition failure logs a warning and contributes no items (never raises).

**3. Config** (`src/utils/config.py`): a new `InsightsConfig` dataclass (`auto_extract: bool = True`) wired into `AppConfig` + `load_config`, mirroring `ActionItemsConfig`. Documented in `config.example.yaml`.

**4. Pipeline** (`src/pipeline_runner.py`): new `_extract_insights(meeting_id, transcript, is_reprocess)` called from `_post_process_async` (gated by `self._config.insights.auto_extract`, own try/except). It loads enabled definitions, runs the extractor off the loop (`asyncio.to_thread`), then `replace_results_for_meeting` (which inherently handles reprocess — no separate branch needed). Skips cleanly when there are no definitions.

**5. API** (`src/api/routes/insights.py`, new; registered in `src/api/server.py` like other routers):

- `GET/POST /api/insight-definitions`, `PATCH/DELETE /api/insight-definitions/{id}` — definition CRUD (validated Pydantic bodies).
- `GET /api/meetings/{id}/insights` — results for a meeting, grouped by definition name.
- Under bearer auth like every route.

**6. UI**:

- **Settings**: a "Custom Insights" panel to list/add/edit/delete definitions (name + prompt + enabled), mirroring however trackers are managed today.
- **Meeting detail**: an "Insights" results section grouping items under each definition name (rendered near the existing summary/insights area). New `api.ts` fns `getInsightDefinitions`/`createInsightDefinition`/`updateInsightDefinition`/`deleteInsightDefinition`/`getMeetingInsights`; new `InsightDefinition`/`MeetingInsightResult` types.

## Data flow

Meeting completes → `_post_process_async` → `_extract_insights`: load enabled definitions → LLM extracts a list per definition → `replace_results_for_meeting` (idempotent, reprocess-safe). UI reads `GET /api/meetings/{id}/insights` and renders grouped lists. User manages definitions in Settings; new definitions apply to future meetings (and to past ones on reprocess).

## Error handling

- Extractor is fully guarded: a bad LLM response for one definition yields no items for it, never breaks the others or the pipeline.
- The whole stage is one try/except in `_post_process_async` (non-fatal, matching siblings).
- Deleting a definition leaves past results intact (they carry `definition_name`); `results_for_meeting` groups by the stored name.

## Testing

- **Repository**: definition CRUD round-trip; `replace_results_for_meeting` deletes + inserts (reprocess replaces, not appends); `results_for_meeting` grouping.
- **Extractor** (fake `Summariser`): parses a JSON list into items; fenced-JSON tolerated; a raising/garbage response for one definition contributes nothing and doesn't stop others; empty definitions → no call.
- **Migration v16**: fresh install + from-v15 upgrade have both tables (literal `user_version = 16`).
- **Pipeline**: `_extract_insights` replaces results on reprocess; skips when no definitions; gated by config.
- **API**: definition CRUD (200/404/422) + `GET /meetings/{id}/insights` shape.
- **UI**: settings add/delete definition PATCHes the right endpoints; meeting insights panel renders grouped results (test the extracted presentational pieces to avoid a brittle full-MeetingDetail render, as in Feature 1).

## Out of scope (later)

- Structured-field / custom-record insights (this ships list-of-items only).
- The **automations engine** (Feature 3), which will be able to _use_ insights as an action.
- Speaker/timestamp linking beyond an optional speaker string (no transcript-anchor UI this pass).
