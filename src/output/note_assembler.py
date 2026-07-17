"""Compose the Markdown note body from a NoteContext.

A pre-enrichment note keeps a light verbatim body (the summary as written,
plus a footer and the transcript per mode). The enriched re-render composes
the gold-skeleton body: the summary's narrative sections mapped into the
vault's heading set, interleaved with writer-computed sections (Related,
Meeting overview, Action items, Insights, Talk time, My Tasks).
"""

from __future__ import annotations

import re
import time

from src.output.note_context import NoteContext

_PRIORITY_EMOJI = {"urgent": "🔺", "high": "⏫", "medium": "🔼", "low": "🔽"}
_INCOMPLETE = {"open", "in_progress"}

# Legacy summary heading -> gold-skeleton heading. Headings not listed and not
# dropped pass through unchanged (tolerant of custom/Ollama templates).
_HEADING_MAP = {
    "summary": "Executive summary",
    "executive summary": "Executive summary",
    "discussion points": "Discussion points",
    "key decisions": "Decisions made",
    "decisions made": "Decisions made",
    "open questions": "Open questions",
    "open questions & risks": "Open questions",  # legacy merged heading
    "risks and blockers": "Risks and blockers",
    "notable quotes": "Notable quotes",
    "next steps": "Next steps",
}
# Sections the writer now owns; dropped from the narrative passthrough.
_DROP = {"participants", "action items", "tags"}

_NARRATIVE_ORDER = [
    "Executive summary",
    "Discussion points",
    "Decisions made",
    "Open questions",
    "Risks and blockers",
    "Notable quotes",
    "Next steps",
]


# ---------------------------------------------------------------------------
# Owner tasks
# ---------------------------------------------------------------------------


def format_my_task(item) -> str:
    """Render one owner task as a Tasks-plugin checkbox line.

    Format: ``- [ ] <title> [#client/x] [#project/y] <emoji> [📅 due]``.
    The client/project tags are what the Meeting Action Items dashboard
    query (contains "#client/" or "#project/") matches on. A meeting whose
    client resolves to a flat tag (for example the owner's own
    ``qvccs-internal``) and that has no project therefore carries neither a
    ``#client/`` nor a ``#project/`` tag, so those owner tasks stay off the
    client/project dashboard by design; give such a meeting a project to
    surface its tasks there.
    """
    parts = [f"- [ ] {item.title.strip()}"]
    if item.client_tag:
        parts.append(f"#{item.client_tag}")
    if item.project_tag:
        parts.append(f"#{item.project_tag}")
    parts.append(_PRIORITY_EMOJI.get(item.priority, "🔼"))
    if item.due_date:
        parts.append(f"📅 {item.due_date}")
    return " ".join(parts)


def render_my_tasks(items) -> str:
    """Render the ## My Tasks section, or "" when there are no owner tasks."""
    if not items:
        return ""
    lines = ["## My Tasks", ""]
    lines += [format_my_task(i) for i in items]
    return "\n".join(lines)


def select_owner_tasks(items, owner_identities, owner_display_name) -> list:
    """Filter to the owner's own incomplete action items.

    An item belongs to the owner when its assignee matches an owner
    identity or the owner's display name (case-insensitively). Completed or
    cancelled items are excluded.
    """
    ident = {i.strip().lower() for i in owner_identities if i and i.strip()}
    ident.add((owner_display_name or "").strip().lower())
    out = []
    for item in items:
        assignee = (item.assignee or "").strip().lower()
        if assignee and assignee in ident and item.status in _INCOMPLETE:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------


def render_transcript(transcript, mode: str) -> str:
    """Render the transcript block for the given mode.

    ``inline`` emits a ## Full Transcript section; ``foldout`` wraps it in a
    collapsible ``> [!quote]-`` callout; ``omit`` and ``linked`` emit nothing
    here (``linked`` is handled by the writer, which knows the vault path).
    """
    if transcript is None or mode in ("omit", "linked"):
        return ""
    rows = []
    for seg in transcript.segments:
        speaker = f" *{seg.speaker}*:" if seg.speaker else ""
        rows.append(f"**{seg.timestamp}**{speaker} {seg.text.strip()}")
    if mode == "foldout":
        body = "\n".join(f"> {r}" for r in rows)
        return "> [!quote]- Full transcript\n" + body
    # inline
    return "## Full Transcript\n\n" + "\n\n".join(rows)


# ---------------------------------------------------------------------------
# Section split and canonicalisation
# ---------------------------------------------------------------------------


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split on level-2 (##) headings into ordered (heading, body) pairs.

    Content before the first ## heading (including the H1) is not returned;
    the enriched assembler regenerates the H1 and falls back to the whole
    body when no ## sections are present.
    """
    out: list[tuple[str, str]] = []
    current: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        m = re.match(r"^##\s+(?!#)(.*)$", line)
        if m:
            if current is not None:
                out.append((current, "\n".join(buf).strip()))
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip()))
    return out


def canonical_heading(heading: str) -> str | None:
    """Map a summary heading to its gold heading, or None to drop it."""
    key = heading.strip().lower()
    if key in _DROP:
        return None
    return _HEADING_MAP.get(key, heading.strip())


# ---------------------------------------------------------------------------
# Rendered sections
# ---------------------------------------------------------------------------


def _cell(value) -> str:
    """Make a value safe for a Markdown table cell or callout line.

    Escapes pipes (which would add spurious columns) and flattens newlines
    (which would break the row / callout after its first line).
    """
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _fmt_hms(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


def _pretty_date(started_at: float, fallback: str) -> str:
    if not started_at:
        return fallback
    lt = time.localtime(started_at)
    # Build without a platform-specific %-d directive.
    return f"{time.strftime('%A, ', lt)}{int(time.strftime('%d', lt))}{time.strftime(' %B %Y', lt)}"


def render_related(ctx: NoteContext) -> str:
    if not ctx.related_links:
        return ""
    lines = ["## Related"]
    for label, name in ctx.related_links:
        lines.append(f"- {label}: [[{name}]]")
    return "\n".join(lines)


def render_overview(ctx: NoteContext) -> str:
    lines = ["## Meeting overview", "", "| Field | Detail |", "|---|---|"]
    lines.append(f"| Date | {_cell(_pretty_date(ctx.started_at, ctx.date))} |")
    lines.append(f"| Duration | ~{ctx.duration_minutes} minutes |")
    if ctx.attendees:
        lines.append(f"| Attendees | {_cell(', '.join(ctx.attendees))} |")
    return "\n".join(lines)


def _callout(kind: str, body: str) -> str:
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in body.splitlines())
    return f"> [!{kind}]\n{quoted}"


def render_action_items(ctx: NoteContext) -> str:
    if not ctx.action_items:
        return ""
    lines = ["## Action items", "", "| Action | Owner | Due | Status |", "|---|---|---|---|"]
    for it in ctx.action_items:
        due = _cell(it.due_date or "Not specified")
        owner = _cell(it.assignee or "Unassigned")
        status = _cell(it.status.replace("_", " ").capitalize())
        lines.append(f"| {_cell(it.title)} | {owner} | {due} | {status} |")
    detail = [it for it in ctx.action_items if it.description]
    if detail:
        lines += ["", "> [!note]- Action item detail"]
        for it in detail:
            lines.append(f"> **{_cell(it.title)}**: {_cell(it.description)}")
    return "\n".join(lines)


def render_talk_time(talk_stats: dict) -> str:
    speakers = (talk_stats or {}).get("speakers") or []
    if not speakers:
        return ""
    lines = ["## Talk time", "", "| Speaker | Talk time | Turns |", "|---|---|---|"]
    for s in speakers:
        lines.append(
            f"| {_cell(s['speaker'])} | {_fmt_hms(s.get('seconds', 0.0))} | {s.get('turns', 0)} |"
        )
    return "\n".join(lines)


def _section(heading: str, body: str | None) -> str:
    return f"## {heading}\n\n{body}" if body and body.strip() else ""


# ---------------------------------------------------------------------------
# Body assembly
# ---------------------------------------------------------------------------


def _footer(ctx: NoteContext) -> str:
    return (
        f"---\n\n*Generated by Context Recall on {time.strftime('%Y-%m-%d %H:%M')}, "
        f"{ctx.duration_minutes} min, {ctx.word_count:,} words*"
    )


def _transcript_tail(ctx: NoteContext, transcript_link: str | None) -> str:
    if ctx.transcript_mode == "linked" and transcript_link:
        return f"## Transcript\n\n- [[{transcript_link}]]"
    return render_transcript(ctx.transcript, ctx.transcript_mode)


def assemble_body(ctx: NoteContext, transcript_link: str | None = None) -> str:
    if not ctx.enriched:
        return _assemble_simple(ctx, transcript_link)
    return _assemble_enriched(ctx, transcript_link)


def _assemble_simple(ctx: NoteContext, transcript_link: str | None) -> str:
    """Light pre-enrichment body: the summary verbatim, footer, transcript."""
    parts = [ctx.summary_markdown.rstrip(), "", "---", "", _footer(ctx).split("---\n\n", 1)[-1]]
    tail = _transcript_tail(ctx, transcript_link)
    if tail:
        parts += ["", tail]
    return "\n".join(parts)


def _assemble_enriched(ctx: NoteContext, transcript_link: str | None) -> str:
    from src.output.markdown_writer import render_insights_section  # lazy: avoids a cycle

    sections = split_sections(ctx.summary_markdown)
    narrative: dict[str, str] = {}
    passthrough: list[tuple[str, str]] = []
    for heading, body in sections:
        canon = canonical_heading(heading)
        if canon is None:
            continue
        if canon in _NARRATIVE_ORDER:
            narrative[canon] = body
        else:
            passthrough.append((canon, body))

    # Fallback: an enriched summary with no recognised sections must not lose
    # its content. Treat the body (minus the H1) as the executive summary.
    if not narrative and not passthrough:
        stripped = re.sub(r"^#\s+.*\n?", "", ctx.summary_markdown, count=1).strip()
        if stripped:
            narrative["Executive summary"] = stripped

    blocks: list[str] = [f"# {ctx.title}"]

    def add(text: str) -> None:
        if text and text.strip():
            blocks.append(text.rstrip())

    add(render_related(ctx))
    add(render_overview(ctx))
    add(_section("Executive summary", narrative.get("Executive summary")))
    add(_section("Discussion points", narrative.get("Discussion points")))
    if narrative.get("Decisions made"):
        add("## Decisions made\n\n" + _callout("info", narrative["Decisions made"]))
    add(render_action_items(ctx))
    add(_section("Open questions", narrative.get("Open questions")))
    if narrative.get("Risks and blockers"):
        add("## Risks and blockers\n\n" + _callout("warning", narrative["Risks and blockers"]))
    add(render_insights_section(ctx.insights))
    add(render_talk_time(ctx.talk_stats))
    add(render_my_tasks(ctx.owner_tasks))
    add(_section("Notable quotes", narrative.get("Notable quotes")))
    add(_section("Next steps", narrative.get("Next steps")))
    for heading, body in passthrough:
        add(_section(heading, body))

    add(_footer(ctx))
    add(_transcript_tail(ctx, transcript_link))

    return "\n\n".join(blocks) + "\n"
