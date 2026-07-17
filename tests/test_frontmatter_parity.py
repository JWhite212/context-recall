import yaml

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import NoteContext
from src.utils.config import MarkdownConfig


def _ctx(**kw):
    base = dict(
        recall_id="4f2a",
        title="Morning Standup Call",
        date="2026-07-15",
        time="10:03",
        started_at=1_752_570_180.0,
        duration_minutes=28,
        word_count=4456,
        client_name="QVCCS Internal",
        client_folder="QVCCS Internal",
        client_tag="qvccs-internal",
        project_name="",
        project_tag="",
        meeting_type="Standup",
        attendees=["Jamie White (QVCCS)", "Amelia Lawton (QVCCS)"],
        extra_tags=[],
        enriched=True,
    )
    base.update(kw)
    return NoteContext(**base)


def _cfg(tmp_path):
    return MarkdownConfig(
        enabled=True, vault_path=str(tmp_path), filename_template="{date}_{slug}.md"
    )


def test_attendees_and_tags_round_trip_as_block_lists(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    ctx = _ctx(project_tag="project/siemens-16", extra_tags=["qvccs-internal"])
    path = w.write_note(ctx)
    text = path.read_text(encoding="utf-8")
    # Block-list form (2-space indent), never an inline flow list.
    assert "attendees:\n  - Jamie White (QVCCS)\n  - Amelia Lawton (QVCCS)" in text
    assert "tags:\n  - qvccs-internal\n  - project/siemens-16" in text
    assert "[" not in text.split("---")[1]  # no inline flow list in frontmatter
    fm = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(fm["attendees"], list) and isinstance(fm["tags"], list)
    assert fm["attendees"] == ["Jamie White (QVCCS)", "Amelia Lawton (QVCCS)"]
    assert fm["tags"] == ["qvccs-internal", "project/siemens-16"]
    assert fm["client"] == "QVCCS Internal" and fm["source"] == "context-recall"
    assert fm["recall_id"] == "4f2a" and fm["enriched"] is True
    assert fm["meeting_type"] == "Standup"


def test_time_round_trips_as_string_not_sexagesimal_int(tmp_path):
    path = MarkdownWriter(_cfg(tmp_path)).write_note(_ctx())
    fm = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])
    assert fm["time"] == "10:03"  # quoted, so not parsed as the int 603


def test_pre_enrichment_note_omits_enriched_fields(tmp_path):
    path = MarkdownWriter(_cfg(tmp_path)).write_note(_ctx(enriched=False))
    fm = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])
    assert "client" not in fm and "source" not in fm and "enriched" not in fm
    assert fm["type"] == "meeting-note"
