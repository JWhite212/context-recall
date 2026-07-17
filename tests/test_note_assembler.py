from src.output.note_assembler import assemble_body, canonical_heading, split_sections
from src.output.note_context import ActionItemView, NoteContext


def test_split_sections_by_h2():
    md = "# Title\n\n## Summary\n\nS body\n\n## Key Decisions\n\nD body\n"
    secs = dict(split_sections(md))
    assert secs["Summary"].strip() == "S body"
    assert secs["Key Decisions"].strip() == "D body"


def test_split_sections_ignores_h3():
    md = "## Discussion Points\n\n### Topic\n\nbody\n"
    secs = dict(split_sections(md))
    assert "Discussion Points" in secs and "Topic" not in secs
    assert "### Topic" in secs["Discussion Points"]


def test_canonical_heading_maps_and_drops():
    assert canonical_heading("Summary") == "Executive summary"
    assert canonical_heading("Key Decisions") == "Decisions made"
    assert canonical_heading("Participants") is None
    assert canonical_heading("Action Items") is None
    assert canonical_heading("Tags") is None
    assert canonical_heading("Executive summary") == "Executive summary"


def _enriched_ctx(**kw):
    base = dict(
        recall_id="m1",
        title="Standup",
        date="2026-07-15",
        time="10:03",
        started_at=1_752_570_180.0,
        duration_minutes=28,
        word_count=10,
        enriched=True,
        attendees=["Jamie White (QVCCS)"],
        transcript_mode="omit",
    )
    base.update(kw)
    return NoteContext(**base)


def test_assemble_body_orders_gold_skeleton():
    ctx = _enriched_ctx(
        summary_markdown=(
            "# Standup\n\n## Summary\n\nWe met.\n\n## Discussion Points\n\n"
            '### Topic\n\nBody.\n\n## Notable Quotes\n\n> "hi" - Me\n'
        ),
        action_items=[
            ActionItemView(title="Do X", assignee="Amelia", status="open", due_date="2026-07-18")
        ],
        owner_tasks=[
            ActionItemView(title="My thing", project_tag="project/siemens-16", priority="medium")
        ],
        talk_stats={
            "speakers": [{"speaker": "Jamie White (QVCCS)", "seconds": 724.0, "turns": 34}],
            "total_speaking_seconds": 724.0,
        },
        insights=[{"definition_name": "Risks", "content": "A risk"}],
        related_links=[("Previous", "2026-07-14 - Standup")],
    )
    body = assemble_body(ctx)
    markers = [
        "## Related",
        "## Meeting overview",
        "## Executive summary",
        "## Discussion points",
        "## Action items",
        "## Insights",
        "## Talk time",
        "## My Tasks",
        "## Notable quotes",
    ]
    order = [body.index(m) for m in markers]
    assert order == sorted(order), body
    assert "| Jamie White (QVCCS) | 12m 04s | 34 |" in body
    assert "- Previous: [[2026-07-14 - Standup]]" in body
    assert "- [ ] My thing #project/siemens-16 🔼" in body
    assert "—" not in body


def test_decisions_and_risks_render_as_callouts():
    ctx = _enriched_ctx(
        summary_markdown=(
            "## Key Decisions\n\n- Ship it\n\n## Risks and blockers\n\n- It might break\n"
        ),
    )
    body = assemble_body(ctx)
    assert "## Decisions made" in body and "> [!info]" in body
    assert "## Risks and blockers" in body and "> [!warning]" in body


def test_action_items_table_plus_collapsible_detail():
    ctx = _enriched_ctx(
        action_items=[
            ActionItemView(
                title="Do X",
                assignee="Amelia",
                due_date="2026-07-18",
                status="open",
                description="Because reasons.",
            )
        ],
    )
    body = assemble_body(ctx)
    assert "| Action | Owner | Due | Status |" in body
    assert "| Do X | Amelia | 2026-07-18 | Open |" in body
    assert "> [!note]- Action item detail" in body
    assert "Because reasons." in body


def test_pre_enrichment_body_is_verbatim_not_restructured():
    ctx = NoteContext(
        recall_id="m1",
        title="T",
        date="2026-07-15",
        time="10:03",
        started_at=0.0,
        duration_minutes=1,
        word_count=1,
        enriched=False,
        summary_markdown="# T\n\nJust a plain draft with no sections.\n",
        transcript_mode="omit",
    )
    body = assemble_body(ctx)
    assert "Just a plain draft with no sections." in body  # nothing dropped
    assert "## Meeting overview" not in body


def test_enriched_summary_without_sections_keeps_content():
    ctx = _enriched_ctx(summary_markdown="# Standup\n\nA freeform recap with no headings.\n")
    body = assemble_body(ctx)
    assert "A freeform recap with no headings." in body
