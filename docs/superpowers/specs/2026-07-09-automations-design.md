# Automations Engine — Design (Feature 3 of 3, Track C)

**Date:** 2026-07-09
**Branch:** `feat/competitor-features` (local-only)
**Gap addressed:** G1 (Automations / rules engine) from `docs/superpowers/specs/2026-07-08-competitor-gap-analysis.md` — Circleback's flagship feature.

## Goal

User-defined **rules** that watch each processed meeting and, when its
conditions match, run a small set of **actions** — apply tags, POST a
webhook, or raise a notification. Rules are managed in Settings, evaluated
automatically at the end of post-processing (and on reprocess), and
reprocess-safe (side-effecting actions fire at most once per meeting).

This compounds with the two features already shipped on this branch:
per-meeting **templates** (F1) structure the notes and **custom insights**
(F2) extract the structured data — automations **route** the result.

## Non-goals (v1, explicit YAGNI)

- **No** run-template / extract-insight actions — templates (F1) and insights
  (F2) already auto-run on every meeting, so re-running them as actions is
  redundant.
- **No** export or send-email actions in v1 (the webhook is the local-first
  escape hatch → Zapier/Make/Slack/Notion). Both are natural v2 additions.
- **No** nested boolean groups — conditions are a flat list combined by a
  single `all` (AND) / `any` (OR) toggle.
- **No** manual "run rules now" trigger — rules fire on the automatic
  post-processing pass and on reprocess only.

## Architecture

Mirrors the trackers/insights subsystems already in the codebase:

```
AutomationRepository ── rules CRUD + dispatch dedupe (automation_rules, automation_dispatches)
RuleEvaluator        ── PURE matcher: (meeting-context, rule) -> bool   (no I/O, no LLM, no DB)
ActionExecutor       ── runs matched actions, reusing existing primitives
_run_automations     ── pipeline stage at the end of _post_process_async
automations route    ── /api/automation-rules CRUD + GET /api/meetings/{id}/automations
AutomationsSection   ── Settings panel (rule builder)
meeting "fired" pills ── on the meeting view
```

Each unit has one clear purpose and a well-defined interface; the evaluator
is pure so the matching logic is exhaustively unit-testable without a DB or
network.

## Data model (DB v16 → v17)

Two new tables. `SCHEMA_VERSION` 16 → 17; add a `if current_version < 17:`
block after the v16 block (move the trailing `else:`), a literal
`PRAGMA user_version = 17`, and the two `executescript`s to the fresh-install
(`< 1`) block — same pattern the v16 insights migration used.

```sql
CREATE TABLE IF NOT EXISTS automation_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_mode TEXT NOT NULL DEFAULT 'all',   -- 'all' (AND) | 'any' (OR)
    conditions_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_dispatches (
    rule_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (rule_id, meeting_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_automation_dispatches_meeting
    ON automation_dispatches(meeting_id);
```

- `conditions_json`: JSON list of `{"field": ..., "value": ...}`.
- `actions_json`: JSON list of `{"type": ..., ...params}`.
- `automation_dispatches` records that a rule's **side-effecting** actions have
  already fired for a meeting, so a reprocess does not re-POST/re-notify. Like
  `notification_dispatches`. `rule_id` has **no** FK (deleting a rule keeps its
  historical dispatch rows harmlessly); `meeting_id` cascades.

## Conditions (evaluated by `RuleEvaluator`, pure)

Meeting-context built from the meeting row:
`{tags: list[str], client_id: str|None, project_id: str|None, title: str, attendee_domains: list[str]}`
(`attendee_domains` derived by parsing `attendees_json` emails → the part after `@`, lowercased).

| `field`           | Matches when …                                     |
| ----------------- | -------------------------------------------------- |
| `tag`             | `value` is in the meeting's `tags`                 |
| `client`          | `value` equals the meeting's `client_id`           |
| `project`         | `value` equals the meeting's `project_id`          |
| `title_contains`  | `value` is a case-insensitive substring of `title` |
| `attendee_domain` | `value` (lowercased) is in `attendee_domains`      |

- The operator is **implied by the field** — a condition is just
  `{field, value}`. Multiplicity ("client A or client B") is expressed with
  multiple condition rows plus `match_mode: any`.
- `match_mode = all` → every condition must match (AND). `any` → at least one
  (OR). An empty condition list matches nothing (a rule must have ≥1 condition;
  enforced at the API layer).
- Unknown `field` values evaluate to `false` (forward-compatible / defensive).

## Actions (`ActionExecutor`)

| `type`      | Params           | Behaviour                                                                            | Idempotent? |
| ----------- | ---------------- | ------------------------------------------------------------------------------------ | ----------- |
| `apply_tag` | `{tags: [str]}`  | Add each tag to `meetings.tags` (dedupe), persist via `repo.update_meeting(tags=…)`  | **Yes**     |
| `webhook`   | `{url, format?}` | Build a per-rule `WebhookChannelConfig(url, format)`, call existing `send_webhook()` | No          |
| `notify`    | `{message?}`     | Raise an in-app + macOS notification via the existing notification channels          | No          |

- **`apply_tag` always runs** for a matched rule (idempotent — reprocess-safe
  by construction).
- **`webhook` / `notify` are side-effecting**, gated by the dispatch table.
  For a matched rule the executor reads `already = has_dispatched(rule_id,
meeting_id)` **before** doing anything, then runs the side-effecting actions
  only when `already` is false. It **records the dispatch for every matched
  rule** (upsert — `INSERT OR IGNORE`), regardless of action type. Because
  presence is checked before the upsert, side-effecting actions fire exactly
  once across the first run and any number of reprocesses — and the dispatch
  table doubles as the authoritative "which rules fired on this meeting" record
  (so even a tag-only rule shows up in the fired list). This is the crux of
  reprocess-safety.
- The webhook payload reuses the existing `send_webhook` contract (title/body/
  type); title = meeting title, body = a short summary + which rule fired.
- All blocking HTTP (webhook) stays off the API event loop (async httpx /
  `asyncio.to_thread`), matching every other post-processing stage.

## Pipeline integration

Add `_run_automations(meeting_id)` and call it as the **last** guarded block in
`_post_process_async` (after tagging + insights, so the meeting is fully
tagged/assigned/summarised before rules evaluate):

```python
try:
    autos_cfg = getattr(self._config, "automations", None)
    if autos_cfg and autos_cfg.enabled:
        await self._run_automations(meeting_id)
except Exception:
    logger.warning("Automations run failed", exc_info=True)
```

`_run_automations`: load enabled rules; if none, return. Build the
meeting-context from `repo.get_meeting`. For each rule, `RuleEvaluator.matches`;
for matches, run `apply_tag` always, run side-effecting actions only when the
rule was not already dispatched for this meeting, then record the dispatch for
the matched rule. Emit `automations.fired` (meeting_id, rule names) when ≥1 rule
matched. Non-fatal (its own try/except, like the other stages).

Note: `is_reprocess` is **not** threaded in — reprocess-safety comes entirely
from `apply_tag` idempotency + the dispatch dedupe, so the same code path is
correct on first-run and reprocess.

## Config

```python
@dataclass
class AutomationsConfig:
    enabled: bool = True
```

Wired into `AppConfig` (after `insights`) and `load_config` via
`_build_dataclass`; commented block added to `config.example.yaml`. (The
`_build_dataclass` None-tolerance fix from F2's review already guards an empty
`automations:` section.)

## API

`src/api/routes/automations.py` (mirror `insights.py`), registered in
`server.py`:

- `GET    /api/automation-rules` — list rules
- `POST   /api/automation-rules` — create (validates name, ≥1 condition, ≥1 action)
- `PATCH  /api/automation-rules/{id}` — update (name/enabled/match_mode/conditions/actions)
- `DELETE /api/automation-rules/{id}` — delete (dispatch history preserved)
- `GET    /api/meetings/{id}/automations` — rule names that fired for the meeting (join `automation_dispatches` → `automation_rules`), 404 if the meeting is unknown

Distinct from any existing route. Pydantic models validate the condition
`field` enum and action `type` enum so malformed rules are rejected at the edge.

## UI

- **Settings `AutomationsSection`** (`ui/src/components/settings/AutomationsSection.tsx`,
  own file, mirrors `InsightsSection`): list rules; create/edit a rule with
  name, enabled `Toggle`, match `all`/`any` select, a **conditions builder**
  (rows of `field` select + value input) and an **actions builder** (rows of
  `type` select + value input(s)). Stored as JSON via the api client. This is
  the largest single UI unit in Track C; kept to flat row-lists to stay
  testable and within one focused file.
- **Meeting view**: a small "Automations: `<rule>` …" pill row rendered by a
  pure `AutomationBadges({ names })` component (like F2's `InsightResults`),
  fed by `getMeetingAutomations(meetingId)`. Rendered inside `MeetingInsights`.
- **UI test lesson (F1/F2):** importing `Settings.tsx`/`MeetingDetail.tsx` in a
  vitest breaks module-load collection — every testable unit lives in its own
  self-contained file and is tested by direct `render()`.

## Testing

- `RuleEvaluator` — pure unit tests across all fields, `all` vs `any`, empty
  conditions, unknown field (the bulk of the correctness coverage; no DB/LLM).
- `AutomationRepository` — rules CRUD; `record_dispatch`/`has_dispatched`
  dedupe; dispatches survive rule deletion; a `fired_rules_for_meeting` join.
- `ActionExecutor` — `apply_tag` dedupe/persist (fake repo); webhook/notify
  invoked via patched channel functions; dispatch dedupe skips side-effects the
  second time.
- Pipeline — `_run_automations` fires a matching rule and records a dispatch;
  disabled in other pipeline tests via `config.automations.enabled = False` in
  the shared `_make_config`.
- API — rule lifecycle (201/200/patch/delete), validation rejects empty
  conditions/actions, `/meetings/{id}/automations` 404 + fired-list.
- UI — `AutomationBadges` pure render; `AutomationsSection` renders existing
  rules + create calls the API (QueryClient + Toast wrappers).

## Reprocess semantics (summary)

| Action      | First run       | Reprocess                                  |
| ----------- | --------------- | ------------------------------------------ |
| `apply_tag` | applies tags    | re-applies (idempotent dedupe — no change) |
| `webhook`   | fires + records | **skipped** (dispatch exists)              |
| `notify`    | fires + records | **skipped** (dispatch exists)              |

## Build sequence (≈11 TDD tasks, one commit each, CodeRabbit gate)

1. DB **v17** — `automation_rules` + `automation_dispatches` + migration.
2. `AutomationRepository` — rules CRUD + `record_dispatch`/`has_dispatched` +
   `fired_rules_for_meeting`.
3. `RuleEvaluator` — pure matcher.
4. `ActionExecutor` — apply_tag / webhook / notify (reuse existing primitives) +
   dispatch dedupe.
5. `AutomationsConfig` + example.yaml.
6. Pipeline `_run_automations` stage.
7. API route + server registration.
8. UI types + api client.
9. Settings `AutomationsSection` (rule builder).
10. Meeting fired-automations display (`AutomationBadges`).
11. Verification (full pytest + ruff + UI + tsc) + CodeRabbit gate.
