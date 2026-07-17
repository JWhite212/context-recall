"""Regression tests for defects found in the whole-branch adversarial review."""

from pathlib import Path

import yaml

from src.output.markdown_writer import MarkdownWriter
from src.output.note_assembler import assemble_body, render_action_items
from src.output.note_context import ActionItemView, NoteContext
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _cfg(tmp_path, **kw):
    base = dict(
        enabled=True,
        vault_path=str(tmp_path),
        route_by_client=True,
        filename_template="{date}_{slug}.md",
    )
    base.update(kw)
    return MarkdownConfig(**base)


def _ctx(**kw):
    base = dict(
        recall_id="m1",
        title="Weekly Review",
        date="2026-07-15",
        time="10:03",
        started_at=1_752_570_180.0,
        duration_minutes=28,
        word_count=10,
        client_folder="Siemens",
        enriched=True,
    )
    base.update(kw)
    return NoteContext(**base)


# --- Findings 1 & 6: reprocess with a client change must not duplicate ---


def test_reprocess_client_change_leaves_single_note(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    # First processing: enriched note filed under Siemens/.
    orig = w.write_note(_ctx(client_folder="Siemens", enriched=True))
    assert orig.parent.name == "Siemens"

    # Reprocess pass 1 (pre-enrichment) reuses the stored path: stays in Siemens/.
    p1 = w.write_note(_ctx(client_folder="Siemens", enriched=False), reuse_path=orig)
    assert p1 == orig

    # Reprocess pass 2 (enriched) after the client was reassigned to NTT.
    p2 = w.write_note(_ctx(client_folder="NTT", enriched=True), reuse_path=p1)
    assert p2.parent.name == "NTT"
    assert not orig.exists()  # old client-folder copy removed
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


# --- Finding 11: pipe / newline in table cells must not break the table ---


def test_action_item_pipe_in_title_is_escaped():
    ctx = _ctx(
        action_items=[
            ActionItemView(
                title="Update staging config | rollback plan",
                assignee="Sarah",
                due_date="2026-07-20",
                status="open",
                description="Line one\nLine two",
            )
        ]
    )
    table = render_action_items(ctx)
    # The one data row must still have exactly 4 columns: escaped pipes (\|)
    # do not count as column separators.
    data_row = [ln for ln in table.splitlines() if ln.startswith("| Update")][0]
    assert "\\|" in data_row
    assert data_row.replace("\\|", "").count("|") == 5
    # Multi-line description collapses to one callout line.
    detail_line = [ln for ln in table.splitlines() if ln.startswith("> **Update")][0]
    assert "Line one Line two" in detail_line


# --- Findings 8 & 10: linked companion must relocate with the note ---


def test_linked_companion_relocates_on_client_change(tmp_path):
    seg = TranscriptSegment(start=0, end=2, text="Hi", speaker="Me")
    transcript = Transcript(segments=[seg], language="en", duration_seconds=2.0)
    w = MarkdownWriter(_cfg(tmp_path))
    first = w.write_note(
        _ctx(client_folder="Siemens", transcript=transcript, transcript_mode="linked")
    )
    old_companion = first.with_name(f"{first.stem} (transcript){first.suffix}")
    assert old_companion.exists()

    second = w.write_note(
        _ctx(client_folder="NTT", transcript=transcript, transcript_mode="linked"),
        reuse_path=first,
    )
    new_companion = second.with_name(f"{second.stem} (transcript){second.suffix}")
    assert second.parent.name == "NTT"
    assert new_companion.exists()
    assert not old_companion.exists()  # old companion moved, not orphaned
    # Exactly two notes: the main note and its one companion.
    assert len(list(Path(tmp_path).rglob("*.md"))) == 2


# --- Finding 9: empty lists must not serialise as inline flow lists ---


def test_empty_tags_and_attendees_are_omitted_not_flow_lists(tmp_path):
    ctx = _ctx(client_folder="Unsorted", attendees=[], extra_tags=[], client_tag="", project_tag="")
    path = MarkdownWriter(_cfg(tmp_path)).write_note(ctx)
    text = path.read_text(encoding="utf-8")
    front = text.split("---\n")[1]
    assert "[]" not in front  # no inline flow list anywhere in frontmatter
    fm = yaml.safe_load(front)
    assert "attendees" not in fm and "tags" not in fm


def test_non_empty_tags_still_block_list(tmp_path):
    ctx = _ctx(
        extra_tags=["qvccs-internal"], client_tag="client/siemens", attendees=["Jamie White"]
    )
    text = MarkdownWriter(_cfg(tmp_path)).write_note(ctx).read_text(encoding="utf-8")
    assert "tags:\n  - qvccs-internal\n  - client/siemens" in text
    assert "attendees:\n  - Jamie White" in text


# --- Assemble body still renders the escaped table end to end ---


def test_assemble_body_table_stays_four_columns_with_pipe():
    ctx = _ctx(
        action_items=[ActionItemView(title="A | B", assignee="X", status="open")],
        summary_markdown="## Executive summary\n\nBody.\n",
    )
    body = assemble_body(ctx)
    row = [ln for ln in body.splitlines() if ln.startswith("| A ")][0]
    assert row.replace("\\|", "").count("|") == 5
