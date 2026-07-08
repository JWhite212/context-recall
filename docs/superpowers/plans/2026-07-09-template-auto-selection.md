# Template Auto-Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-select the best summary template per meeting via an LLM classifier (manual override wins), persist which template was used, and surface it in the existing UI dropdown.

**Architecture:** A new `TemplateSelector` (mirrors `src/tagging/assigner.py`'s `LlmAssigner`) runs in `PipelineRunner` _before_ summarising, choosing from `TemplateManager.list_templates()` using title/attendees/transcript. Selection precedence is **manual → auto → default**. The chosen template name + source persist on the meeting; the resummarise route records manual overrides; the UI seeds its existing template `<select>` from the persisted value.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, aiosqlite, FastAPI; React 19 + TS + Vitest.

## Global Constraints

- **British spelling** in new comments/log messages.
- **pytest-asyncio auto mode** — no `@pytest.mark.asyncio` needed, but this repo's tests use it explicitly; match the file you edit.
- DB tests use the conftest `db`/`repo` fixtures; migrations run inside `Database.connect()`.
- **Head `SCHEMA_VERSION` on this branch is 14** → new columns land at **v15**. When bumping: change the `if current_version < 14` block's terminal `PRAGMA user_version = {SCHEMA_VERSION}` to a **literal `= 14`**, add columns to the fresh-install block _and_ a new `if current_version < 15:` block, and move the trailing `else:` after v15.
- `update_meeting()` raises `ValueError` for any field not in `_MUTABLE_COLUMNS` — add new columns there first.
- `from_row` must guard new columns with `try/except (IndexError, KeyError)` (older DBs).
- The `TemplateSelector` runs **before** summarisation, so it uses title/attendees/**transcript** — never the summary (which doesn't exist yet).
- Non-fatal: any selector failure falls back to `default_template`; the pipeline never fails on selection.
- **Manual never overwritten**: `template_source="manual"` is preserved across auto re-runs/reprocess.
- Lint: `ruff check src/ tests/`; UI: `cd ui && npx tsc --noEmit`. Commits end with the `Claude-Session:` trailer.

---

### Task 1: DB v15 migration + repository fields

**Files:**

- Modify: `src/db/database.py` (`SCHEMA_VERSION` line 25; fresh-install block ~520-532; v14 block ~662-671)
- Modify: `src/db/repository.py` (`_MUTABLE_COLUMNS` ~23-49; `MeetingRecord` + `to_dict` ~52-111; `from_row` ~113-183)
- Test: `tests/test_db_migration_v15.py` (create), `tests/test_repository.py`

**Interfaces:**

- Produces: `meetings.template_name TEXT DEFAULT ''`, `meetings.template_source TEXT DEFAULT ''`; `MeetingRecord.template_name`, `.template_source`; both writable via `update_meeting`.

- [ ] **Step 1: Write the failing migration test** — `tests/test_db_migration_v15.py`:

```python
import json

from src.db.database import Database, SCHEMA_VERSION
from src.db.repository import MeetingRepository


async def test_v15_adds_template_columns(tmp_path):
    db = Database(db_path=tmp_path / "v15.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 15
        cur = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"template_name", "template_source"} <= cols
    finally:
        await db.close()


async def test_v15_round_trips_template_fields(tmp_path):
    db = Database(db_path=tmp_path / "v15b.db")
    await db.connect()
    repo = MeetingRepository(db)
    mid = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(mid, template_name="discovery", template_source="manual")
    m = await repo.get_meeting(mid)
    assert m.template_name == "discovery"
    assert m.template_source == "manual"
    await db.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db_migration_v15.py -v`
Expected: FAIL — `SCHEMA_VERSION` is 14 / columns absent / `update_meeting` rejects `template_name`.

- [ ] **Step 3: Add the columns + migration** (`src/db/database.py`)

1. `SCHEMA_VERSION = 14` → `SCHEMA_VERSION = 15`.
2. In the fresh-install block (where the v13 assignment columns are added via `_safe_add_column`), add:

```python
            await _safe_add_column(self.conn, "meetings", "template_name", "TEXT", "''")
            await _safe_add_column(self.conn, "meetings", "template_source", "TEXT", "''")
```

3. Change the v14 block's terminal PRAGMA to a literal and append a v15 block (mirror the existing style). Read the exact current v14 block first; it is:

```python
        if current_version < 14:
            await self.conn.executescript(TRACKERS_SQL)
            await self.conn.executescript(TRACKER_HITS_SQL)
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 14 (keyword trackers)")
            current_version = 14
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

Replace with:

```python
        if current_version < 14:
            await self.conn.executescript(TRACKERS_SQL)
            await self.conn.executescript(TRACKER_HITS_SQL)
            await self.conn.execute("PRAGMA user_version = 14")
            await self.conn.commit()
            logger.info("Database migrated to version 14 (keyword trackers)")
            current_version = 14

        if current_version < 15:
            # Per-meeting summary template + its source (auto/manual/default).
            await _safe_add_column(self.conn, "meetings", "template_name", "TEXT", "''")
            await _safe_add_column(self.conn, "meetings", "template_source", "TEXT", "''")
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 15 (per-meeting template)")
            current_version = 15
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Add repository fields** (`src/db/repository.py`)

- Add `"template_name", "template_source"` to `_MUTABLE_COLUMNS`.
- Add to `MeetingRecord` (after `assignment_confidence`): `template_name: str = ""` and `template_source: str = ""`.
- Add to `to_dict()`: `"template_name": self.template_name, "template_source": self.template_source,`.
- In `from_row`, mirror the guarded assignment-field block:

```python
        template_name = ""
        template_source = ""
        try:
            template_name = row["template_name"] or ""
            template_source = row["template_source"] or ""
        except (IndexError, KeyError):
            pass
```

and pass `template_name=template_name, template_source=template_source` into the `cls(...)` call.

- [ ] **Step 5: Run to verify it passes + no regression**

Run: `.venv/bin/python -m pytest tests/test_db_migration_v15.py tests/test_db_migration_v14.py tests/test_repository.py -q`
Expected: PASS. (v14 test still green — its DB now stamps user_version 14 then migrates on to 15 via the new block.)

- [ ] **Step 6: Lint + commit**

Run: `.venv/bin/ruff check src/db/database.py src/db/repository.py tests/test_db_migration_v15.py`

```bash
git add src/db/database.py src/db/repository.py tests/test_db_migration_v15.py tests/test_repository.py
git commit -m "feat(db): per-meeting template_name/template_source columns (v15)"
```

---

### Task 2: Config toggles

**Files:**

- Modify: `src/utils/config.py` (`SummarisationConfig` ~134-155)
- Modify: `config.example.yaml` (summarisation block)
- Test: `tests/test_config.py`

**Interfaces:**

- Produces: `SummarisationConfig.auto_select_template: bool = True`, `.template_select_min_confidence: float = 0.6`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_config.py`, matching its style)

```python
def test_summarisation_config_has_template_auto_select_defaults():
    from src.utils.config import SummarisationConfig

    cfg = SummarisationConfig()
    assert cfg.auto_select_template is True
    assert cfg.template_select_min_confidence == 0.6
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k template_auto_select -v`
Expected: FAIL — `AttributeError: auto_select_template`.

- [ ] **Step 3: Add the fields** (`src/utils/config.py`, after `default_template`)

```python
    default_template: str = "standard"  # Fallback template name for summarisation.
    auto_select_template: bool = True  # LLM picks the best template per meeting.
    template_select_min_confidence: float = 0.6  # Below this, keep default_template.
```

Add to `config.example.yaml` under `summarisation:`:

```yaml
# Let an LLM pick the most appropriate template per meeting (falls back to
# default_template below its confidence threshold or on any error).
# auto_select_template: true
# template_select_min_confidence: 0.6
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_config.py
git commit -m "feat(config): auto_select_template + confidence knobs"
```

---

### Task 3: `TemplateSelector`

**Files:**

- Create: `src/template_selection.py`
- Test: `tests/test_template_selection.py` (create)

**Interfaces:**

- Consumes: `SummarisationConfig`, `list[SummaryTemplate]`.
- Produces: `TemplateSelector(summarisation_config).select(title, attendees, transcript_text, templates, default_name, min_confidence) -> str` — returns a template name guaranteed to be in `{t.name for t in templates}` or `default_name`. Never raises.

- [ ] **Step 1: Write the failing tests** — `tests/test_template_selection.py`:

```python
from unittest.mock import MagicMock

from src.template_selection import TemplateSelector
from src.templates import SummaryTemplate
from src.utils.config import SummarisationConfig

_TEMPLATES = [
    SummaryTemplate(name="standard", description="General meeting", system_prompt="x"),
    SummaryTemplate(name="discovery", description="Sales discovery call", system_prompt="y"),
]


def _selector(reply: str):
    sel = TemplateSelector(SummarisationConfig())
    sel._summariser = MagicMock()
    sel._summariser.chat.return_value = reply
    return sel


def test_selects_named_template():
    sel = _selector('{"template": "discovery", "confidence": 0.9}')
    assert sel.select("Acme discovery", [], "we want to explore your needs",
                      _TEMPLATES, "standard", 0.6) == "discovery"


def test_unknown_name_falls_back_to_default():
    sel = _selector('{"template": "nonsense", "confidence": 0.9}')
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_low_confidence_falls_back_to_default():
    sel = _selector('{"template": "discovery", "confidence": 0.2}')
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_llm_exception_falls_back_to_default():
    sel = TemplateSelector(SummarisationConfig())
    sel._summariser = MagicMock()
    sel._summariser.chat.side_effect = RuntimeError("backend down")
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_fewer_than_two_templates_returns_default_without_calling_llm():
    sel = _selector('{"template": "discovery", "confidence": 0.9}')
    one = [_TEMPLATES[0]]
    assert sel.select("x", [], "y", one, "standard", 0.6) == "standard"
    sel._summariser.chat.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_template_selection.py -q`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement** — `src/template_selection.py` (mirrors `LlmAssigner`):

````python
"""LLM selection of the best summary template for a meeting.

Runs before summarisation, so it sees the title, attendees and transcript
(never the summary). Mirrors src/tagging/assigner.py's LlmAssigner: a
one-shot chat call, a fenced-JSON parse, and a graceful fallback to the
configured default template on any failure.
"""

from __future__ import annotations

import json
import logging
import re

from src.summariser import Summariser
from src.templates import SummaryTemplate
from src.utils.config import SummarisationConfig

logger = logging.getLogger(__name__)

TEMPLATE_SELECT_PROMPT = """You choose the most appropriate meeting-notes template.

Given a meeting's title, attendees and a transcript excerpt, plus a list of
available templates (name and description), pick the single best-fitting one.

Return ONLY a JSON object:
- "template": the name of the best template, exactly as given
- "confidence": 0.0-1.0, how certain you are
- "rationale": one short sentence

Rules:
- Pick only from the provided template names. Never invent a name.
- If none clearly fit, pick the most general one with low confidence."""

_TRANSCRIPT_EXCERPT_CHARS = 2000


class TemplateSelector:
    """Picks a template name for a meeting; falls back to a default."""

    def __init__(self, summarisation_config: SummarisationConfig) -> None:
        self._summariser = Summariser(summarisation_config)

    def select(
        self,
        title: str,
        attendees: list[dict],
        transcript_text: str,
        templates: list[SummaryTemplate],
        default_name: str,
        min_confidence: float,
    ) -> str:
        names = {t.name for t in templates}
        if len(names) < 2:
            return default_name
        try:
            response = self._call_llm(title, attendees, transcript_text, templates)
            picked = self._parse(response, names, min_confidence)
            return picked or default_name
        except Exception as e:  # never fail summarisation on selection
            logger.warning("Template selection failed: %s", e)
            return default_name

    def _call_llm(
        self,
        title: str,
        attendees: list[dict],
        transcript_text: str,
        templates: list[SummaryTemplate],
    ) -> str:
        template_lines = [f"- {t.name}: {t.description}" for t in templates]
        attendee_names = ", ".join(
            a.get("name", "") for a in attendees if a.get("name")
        )
        excerpt = transcript_text[:_TRANSCRIPT_EXCERPT_CHARS]
        fence = "=" * 40
        user_msg = (
            "Available templates:\n" + "\n".join(template_lines) + "\n\n"
            f"Meeting title: {title}\n"
            f"Attendees: {attendee_names or 'unknown'}\n\n"
            f"{fence} BEGIN TRANSCRIPT EXCERPT {fence}\n"
            f"{excerpt}\n"
            f"{fence} END TRANSCRIPT EXCERPT {fence}"
        )
        return self._summariser.chat(TEMPLATE_SELECT_PROMPT, user_msg)

    def _parse(self, response: str, names: set[str], min_confidence: float) -> str | None:
        if not response:
            return None
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        name = data.get("template")
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if name in names and confidence >= min_confidence:
            return name
        return None
````

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_template_selection.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + commit**

Run: `.venv/bin/ruff check src/template_selection.py tests/test_template_selection.py`

```bash
git add src/template_selection.py tests/test_template_selection.py
git commit -m "feat(templates): TemplateSelector LLM classifier"
```

---

### Task 4: Pipeline integration (selection precedence + persist)

**Files:**

- Modify: `src/pipeline_runner.py` (template-selection region ~316-353)
- Test: `tests/test_pipeline_runner.py`

**Interfaces:**

- Consumes: `TemplateSelector`, `TemplateManager`, meeting `template_name`/`template_source`.
- Produces: pipeline resolves template as **manual (persisted) → auto (TemplateSelector) → default**, persists `template_name` + `template_source` alongside the summary.

- [ ] **Step 1: Read the exact current region** `src/pipeline_runner.py:310-355` (the `template = None; tm.get_template(default_template)` block through the Step-4 `self._update(...)`), then **Step 2** below writes a test against a helper we extract.

- [ ] **Step 2: Write the failing test** (add to `tests/test_pipeline_runner.py`, matching its runner-construction style)

```python
def test_select_template_prefers_manual(monkeypatch, runner_and_meeting):
    runner, meeting = runner_and_meeting  # meeting.template_source == "manual", template_name == "discovery"
    chosen = runner._select_template(meeting, transcript_text="hello")
    assert chosen.name == "discovery"


def test_select_template_uses_selector_when_auto(monkeypatch, runner_and_meeting_auto):
    runner, meeting = runner_and_meeting_auto  # no manual override, auto_select on
    monkeypatch.setattr(
        "src.pipeline_runner.TemplateSelector",
        lambda cfg: type("S", (), {"select": lambda self, *a, **k: "standup"})(),
    )
    chosen, source = runner._select_template_with_source(meeting, transcript_text="daily standup")
    assert chosen.name == "standup"
    assert source == "auto"
```

> The two fixtures (`runner_and_meeting`, `runner_and_meeting_auto`) build a `PipelineRunner` with a stub config and a `MeetingRecord`; model them on the existing `PipelineRunner` construction in `tests/test_pipeline_runner.py`. Read that file's existing fixtures first and reuse them; the assertions above are the contract.

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_runner.py -k select_template -v`
Expected: FAIL — `_select_template` / `_select_template_with_source` undefined.

- [ ] **Step 4: Implement** — add a helper to `PipelineRunner` and call it from the summarise step. Replace the current template block:

```python
        # Step 3: Summarise.
        template = None
        try:
            tm = TemplateManager()
            template = tm.get_template(self._config.summarisation.default_template)
        except Exception as e:
            logger.warning("Failed to load template: %s", e)
```

with:

```python
        # Step 3: Summarise (per-meeting template: manual -> auto -> default).
        template, template_source = self._select_template_with_source(
            meeting, transcript.text
        )
```

Add the helpers (near the other `_` helpers). `transcript.text` is the joined transcript string — confirm the attribute name when reading the file; if it differs, join segment texts.

```python
    def _select_template_with_source(self, meeting, transcript_text):
        """Resolve (SummaryTemplate, source) for a meeting: manual -> auto -> default."""
        from src.template_selection import TemplateSelector

        sm = self._config.summarisation
        tm = TemplateManager()
        default_name = sm.default_template

        # 1. Manual override persisted on the meeting wins.
        manual = getattr(meeting, "template_source", "") == "manual"
        manual_name = getattr(meeting, "template_name", "") or ""
        if manual and manual_name:
            tpl = tm.get_template(manual_name)
            if tpl:
                return tpl, "manual"

        # 2. LLM auto-selection (best effort).
        if sm.auto_select_template:
            try:
                attendees = json.loads(meeting.attendees_json or "[]")
            except (ValueError, TypeError):
                attendees = []
            chosen = TemplateSelector(sm).select(
                title=meeting.title or "",
                attendees=attendees if isinstance(attendees, list) else [],
                transcript_text=transcript_text or "",
                templates=tm.list_templates(),
                default_name=default_name,
                min_confidence=sm.template_select_min_confidence,
            )
            tpl = tm.get_template(chosen)
            if tpl:
                return tpl, ("auto" if chosen != default_name else "default")

        # 3. Default.
        return tm.get_template(default_name), "default"

    def _select_template(self, meeting, transcript_text):
        return self._select_template_with_source(meeting, transcript_text)[0]
```

Then add to the Step-4 `self._update(meeting_id, ...)` call:

```python
            template_name=template.name if template else "",
            template_source=template_source,
```

Guard: only overwrite `template_source` with `"auto"/"default"` when it is **not** already `"manual"` (a reprocess must not clobber a manual choice). Since `_select_template_with_source` already returns `"manual"` when the meeting has one, persisting the returned source is correct — but confirm reprocess passes the existing `meeting` (with its stored source) into this path.

- [ ] **Step 5: Run to verify it passes + runner suite**

Run: `.venv/bin/python -m pytest tests/test_pipeline_runner.py -q`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
git add src/pipeline_runner.py tests/test_pipeline_runner.py
git commit -m "feat(pipeline): per-meeting template selection (manual->auto->default)"
```

---

### Task 5: Resummarise persists the manual template

**Files:**

- Modify: `src/api/routes/resummarise.py` (final `update_meeting` ~122-140)
- Test: `tests/test_api_resummarise.py`

**Interfaces:**

- Produces: `POST /api/meetings/{id}/resummarise?template_name=X` persists `template_name=X`, `template_source="manual"`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_api_resummarise.py`, reusing its `client` fixture + Summariser mock)

```python
@pytest.mark.asyncio
async def test_resummarise_persists_manual_template(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(mid, transcript_json=json.dumps({"segments": [
        {"start": 0.0, "end": 1.0, "text": "hi", "speaker": "Me"}]}))
    mock_summary = MeetingSummary(title="T", raw_markdown="# T", tags=[])
    with patch("src.api.routes.resummarise._load_summarisation_config"):
        with patch("src.api.routes.resummarise.Summariser") as m:
            m.return_value.summarise.return_value = mock_summary
            resp = c.post(f"/api/meetings/{mid}/resummarise?template_name=standup",
                          headers=_auth_headers())
    assert resp.status_code == 200
    meeting = await repo.get_meeting(mid)
    assert meeting.template_name == "standup"
    assert meeting.template_source == "manual"
```

> Match the exact `MeetingSummary(...)` construction used elsewhere in this test file (read it first — field names must match).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_resummarise.py -k persists_manual -v`
Expected: FAIL — `template_name` not persisted (defaults to "").

- [ ] **Step 3: Implement** — in the success-path `update_meeting(...)` add:

```python
        template_name=template.name if template else meeting.template_name,
        template_source="manual" if template else meeting.template_source,
```

(Only stamp `manual` when a template was explicitly chosen for this re-summarise.)

- [ ] **Step 4: Run to verify it passes + route suite**

Run: `.venv/bin/python -m pytest tests/test_api_resummarise.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
git add src/api/routes/resummarise.py tests/test_api_resummarise.py
git commit -m "feat(api): resummarise records manual template choice"
```

---

### Task 6: UI — surface & seed the template selection

**Files:**

- Modify: `ui/src/lib/types.ts` (`Meeting` interface)
- Modify: `ui/src/components/meetings/MeetingDetail.tsx` (state ~454; select ~822)
- Test: `ui/src/components/meetings/__tests__/TemplateSelection.test.tsx` (create)

**Interfaces:**

- Consumes: `meeting.template_name` (new field), existing `getTemplates()` + `resummariseMeeting(id, name)`.
- Produces: the re-summarise popover's `<select>` defaults to the meeting's applied template; an "auto"/"manual" hint shown.

- [ ] **Step 1: Add the type** — `ui/src/lib/types.ts`, in `Meeting` (near the optional fields):

```ts
  template_name?: string | null;
  template_source?: string | null;
```

- [ ] **Step 2: Write the failing test** — `ui/src/components/meetings/__tests__/TemplateSelection.test.tsx`. Render `MeetingDetail` wrapped in `QueryClientProvider` (retry:false) + `MemoryRouter`, stub `globalThis.fetch` to return a meeting with `template_name: "discovery"` for `/api/meetings/:id`, `[{name:"standard"...},{name:"discovery"...}]` for `/api/templates`, and 200/empty for the other queries (clients/projects/action-items/talk-stats). Open the re-summarise popover and assert the template `<select>` value is `"discovery"`.

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import type { ReactNode } from "react";
import { MeetingDetail } from "../MeetingDetail";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function wrap(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/meetings/m1"]}>
        <Routes>
          <Route path="/meetings/:id" element={node} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("MeetingDetail template selection", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.match(/\/api\/meetings\/m1(\?|$)/)) {
        return jsonResponse({
          id: "m1",
          title: "Acme discovery",
          status: "complete",
          tags: [],
          template_name: "discovery",
          summary_markdown: "# x",
          started_at: 1,
          ended_at: 2,
          duration_seconds: 1,
          transcript_json: "{}",
        });
      }
      if (url.includes("/api/templates")) {
        return jsonResponse([
          {
            name: "standard",
            description: "d",
            system_prompt: "p",
            sections: [],
          },
          {
            name: "discovery",
            description: "d",
            system_prompt: "p",
            sections: [],
          },
        ]);
      }
      return jsonResponse({});
    }) as unknown as typeof fetch;
  });

  it("seeds the re-summarise template select from the meeting's applied template", async () => {
    render(wrap(<MeetingDetail />));
    await waitFor(() => screen.getByText("Acme discovery"));
    // open the re-summarise popover (button label per the existing UI)
    fireEvent.click(screen.getByRole("button", { name: /re-?summarise/i }));
    const select = await screen.findByLabelText(/template/i);
    expect((select as HTMLSelectElement).value).toBe("discovery");
  });
});
```

> Read `MeetingDetail.tsx:795-858` first to match the exact button text and add an `aria-label="template"` to the `<select>` if it lacks one. If `MeetingDetail` isn't a named export, adjust the import.

- [ ] **Step 3: Run to verify it fails**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/TemplateSelection.test.tsx`
Expected: FAIL — select value is `"standard"` (hardcoded), not `"discovery"`.

- [ ] **Step 4: Implement** — in `MeetingDetail.tsx`, seed the select from the meeting. Change the hardcoded state and derive the effective value:
- Keep `const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);`
- Compute the select's `value={selectedTemplate ?? meeting.template_name ?? "standard"}`.
- Ensure the `<select>` has `aria-label="template"`.
- Optionally render a small "auto"/"manual" badge from `meeting.template_source` next to it (mirror `AssignmentSelect`'s badge).

- [ ] **Step 5: Run to verify it passes + typecheck + full UI suite**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/TemplateSelection.test.tsx && npx tsc --noEmit && npm test`
Expected: PASS, no type errors.

- [ ] **Step 6: Commit**

```bash
git add ui/src/lib/types.ts ui/src/components/meetings/MeetingDetail.tsx ui/src/components/meetings/__tests__/TemplateSelection.test.tsx
git commit -m "feat(ui): seed re-summarise template select from applied template"
```

---

### Task 7: Feature verification

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/ -q` → all pass.
- [ ] **Step 2:** `.venv/bin/ruff check src/ tests/` → clean.
- [ ] **Step 3:** `cd ui && npm test && npx tsc --noEmit` → pass.
- [ ] **Step 4:** `coderabbit review --agent --base origin/main` → address Critical/Warning, re-run until clean.
- [ ] **Step 5:** Report per-task test counts + anything not done.

---

## Self-Review

**Spec coverage:** TemplateSelector (Task 3) ✔; pipeline manual→auto→default + persist (Task 4) ✔; DB columns + migration (Task 1) ✔; config toggle (Task 2) ✔; API manual override + re-summarise persist (Task 5) ✔; UI dropdown seeded + badge (Task 6) ✔; testing across all ✔. Seeding example Type templates is left to the user (built-ins already give the selector ≥2 choices).

**Placeholder scan:** New code (TemplateSelector, migration, config, tests) is complete. Three steps say "read the exact current region first" (pipeline_runner block, MeetingDetail popover, MeetingSummary/fixture shapes) because those verbatim spans weren't fully captured in the context map — each names the file, the line range, and the exact contract to satisfy, so they're actionable, not vague.

**Type consistency:** `template_name`/`template_source` are consistent across DB (Task 1), pipeline persist (Task 4), API (Task 5), and UI type (Task 6). `TemplateSelector.select(...)` signature (Task 3) matches its call in Task 4. `_select_template_with_source` returns `(SummaryTemplate, source)` used consistently.
