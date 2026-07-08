# Per-Meeting Template Auto-Selection — Design

**Date:** 2026-07-08
**Branch:** `feat/competitor-features` (off `origin/main`)
**Cluster:** Feature 1 of 3 in the "intelligence + workflow" layer (templates → custom insights → automations). This spec covers **template auto-selection + manual override only**; insights and automations are separate specs.

## Goal

Give each meeting the most appropriate summary template automatically, so different meeting types (e.g. Discovery / Implementation / Review) produce appropriately-structured notes — with a manual per-meeting override. Today the pipeline uses one config-wide `default_template` for every meeting.

## Background (current state)

- `src/templates.py` — `SummaryTemplate` (`name`, `description`, `system_prompt`, `sections`) + `TemplateManager` (5 built-ins + custom YAML on disk). `get_template(name)` / `list_templates()`.
- `src/pipeline_runner.py:317-320` — selects `tm.get_template(config.summarisation.default_template)` for **all** meetings, then `summariser.summarise(transcript, template=..., extra_context=...)`.
- `src/tagging/assigner.py` — the pattern to mirror: `deterministic_assignment(...)` pre-pass + `LlmAssigner` (`assign()` → `_call_llm()` → `summariser.chat(PROMPT, user_msg)` → `_parse()` with fenced-JSON extraction + fallback). Manual assignments are never overwritten by auto.
- `src/api/routes/resummarise.py` — `POST /api/meetings/{id}/resummarise` re-runs summarisation on the stored transcript using `TemplateManager` + `Summariser`.
- `Summariser.chat(system_prompt, user_msg) -> str` exists.

## Architecture

Selection precedence at summarise time: **manual override (if set) → LLM classifier (if enabled) → config default**.

### Components

**1. `TemplateSelector`** — new, `src/template_selection.py`. Mirrors `LlmAssigner`.

- Consumes: `SummarisationConfig` (to build a `Summariser` for `chat`), the meeting `title`, `attendees`, a transcript excerpt, and `templates: list[SummaryTemplate]`.
- Produces: `select(title, attendees, transcript, templates, default_name) -> str` — returns the chosen template **name**.
- Method: build a compact prompt listing each candidate template as `name — description`, plus the meeting title/attendees and a bounded transcript excerpt (e.g. first ~2000 chars); call `summariser.chat(TEMPLATE_SELECT_PROMPT, user_msg)`; parse a JSON object `{"template": "<name>"}` with the same fenced-JSON tolerance as `assigner._parse`.
- **Fallback (non-fatal):** returns `default_name` when — LLM unavailable/raises, response unparseable, or the named template isn't in `templates`. Never raises.
- Only invoked when `>= 2` templates exist (with 0–1 templates there's nothing to choose).

**2. Pipeline integration** — `src/pipeline_runner.py`.

- New helper `_select_template(meeting, transcript) -> SummaryTemplate`:
  - If the meeting row has `template_source == "manual"` and a valid `template_name` → use it.
  - Else if `config.summarisation.auto_select_template` (default `True`) and ≥2 templates exist → `TemplateSelector.select(...)`, persist result with `template_source="auto"`.
  - Else → `config.summarisation.default_template`.
- Replaces the current fixed lookup at 317-320. The resolved template is used for `summariser.summarise(...)`, and `template_name` / `template_source` are written via the existing `DbBridge` update path alongside the summary.

**3. DB migration (v14 on this branch → next)** — `src/db/database.py`.

- Add to `meetings`: `template_name TEXT DEFAULT ''`, `template_source TEXT DEFAULT ''` (mirrors the v13 `assignment_source`/`assignment_confidence` pattern via `_safe_add_column`).
- Add both to `SCHEMA_SQL` (fresh installs) and a new incremental migration block. Repository `_MUTABLE_COLUMNS` gains `template_name`, `template_source`; the `Meeting` dataclass + `from_row` gain the fields.

> Note: on `origin/main` the head schema version differs from the bug-sprint branch (which added v15). This spec targets whatever the head is on `feat/competitor-features` at build time; the plan will read `SCHEMA_VERSION` and use `head + 1`.

**4. Config** — `src/utils/config.py` `SummarisationConfig`: add `auto_select_template: bool = True`. Documented (commented) in `config.example.yaml`. `_build_dataclass` ignores unknown keys, so old configs still load.

**5. API** — manual override reuses the **existing** `POST /api/meetings/{id}/resummarise?template_name=<name>` route (`src/api/routes/resummarise.py`) rather than a new endpoint — it already validates the template (404 unknown), re-runs summarisation synchronously, and returns **200**. The only change is that it now also persists `template_name` + `template_source="manual"` on success.

- 404 if the meeting is missing / no transcript; the route already 404s an unknown template name.
- `GET /api/templates` already lists templates for the dropdown (reuse as-is).

> Implementation note (reconciled 2026-07-09): the original draft proposed a new `PATCH /api/meetings/{id}/template`. The context map showed `resummarise` already accepts `template_name` and returns 200 synchronously, so it was reused — simpler and avoids a duplicate summarise path. There is no `PATCH /template` endpoint and no 202 flow.

**6. UI** — `ui/src/components/meetings/MeetingDetail.tsx`.

- A `TemplateSelect` dropdown near the summary showing the current template (`meeting.template_name`, "auto"/"manual" hint), options from `getTemplates()`. Selecting one calls `setMeetingTemplate(id, name)` → PATCH → re-summarise; show a pending/toast state.
- `ui/src/lib/api.ts`: add `setMeetingTemplate(id, name)`; types gain `template_name` / `template_source`.

## Data flow

Meeting completes transcription → `_select_template` (manual → auto-classify → default) → `summarise` with that template → persist summary + `template_name`/`template_source`. User later picks a different template in the UI → PATCH `/template` (manual) → re-summarise → UI refreshes.

## Error handling

- Classifier is wrapped like other intelligence modules: any failure logs a warning and falls back to `default_template`; the pipeline never fails because of template selection.
- Unknown template name from the LLM → fallback. Unknown name from the API → 422.
- Manual (`template_source="manual"`) is never overwritten by an auto re-run (same rule as tagging).

## Testing

- **`TemplateSelector`** (unit, fake `Summariser.chat`): picks the named template; falls back to default on unknown name, unparseable output, and raised exception; not invoked with <2 templates.
- **Pipeline** (`app_with_mocked_api` / runner tests): manual override respected; auto used when enabled and no manual; default when disabled; chosen name persisted.
- **Migration** test: fold-free column add; head DB has the columns; old row survives.
- **Repository**: `update_meeting(template_name=..., template_source=...)` round-trips.
- **API**: `PATCH /template` 200 + persisted + re-summarise triggered; 404 missing meeting; 422 unknown template.
- **UI** (vitest): dropdown renders current template; selecting one PATCHes `/template` with the right body.

## Out of scope (later specs)

- **Custom AI insights** (Feature 2) and the **automations engine** (Feature 3).
- Authoring the specific Discovery/Implementation/Review template _content_ — user data. The plan will seed 1–2 example built-ins (e.g. a "discovery" template) to demonstrate, but the mechanism is content-agnostic.
- Any cloud/collaboration/CRM surface.
