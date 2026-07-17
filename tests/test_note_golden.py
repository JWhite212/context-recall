from pathlib import Path

import yaml

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import ActionItemView, NoteContext
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _ctx():
    seg = TranscriptSegment(start=0.0, end=2.0, text="Morning all.", speaker="Jamie White (QVCCS)")
    return NoteContext(
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
        project_name="Siemens 16 Smart UK Infrastructure",
        project_tag="project/siemens-16",
        meeting_type="Standup",
        attendees=["Jamie White (QVCCS)", "Amelia Lawton (QVCCS)", "Seb (QVCCS)"],
        extra_tags=["qvccs-internal"],
        summary_markdown=(
            "# Morning Standup Call\n\n## Executive summary\n\nWe reviewed progress.\n\n"
            "## Discussion points\n\n### Callbacks\n\nDiscussed the queue.\n\n"
            "## Decisions made\n\n- Proceed with the Teams queue.\n\n"
            "## Open questions\n\n- Which methodology?\n\n"
            "## Risks and blockers\n\n- Tight timeline.\n\n"
            '## Notable quotes\n\n> "Let us ship it." - Jamie\n'
        ),
        action_items=[
            ActionItemView(
                title="Rebuild callback logic",
                assignee="Jamie",
                status="open",
                due_date="2026-07-18",
                priority="medium",
                client_tag="",
                project_tag="project/siemens-16",
            )
        ],
        owner_tasks=[
            ActionItemView(
                title="Rebuild callback logic",
                project_tag="project/siemens-16",
                priority="medium",
                due_date="2026-07-18",
            )
        ],
        talk_stats={
            "speakers": [{"speaker": "Jamie White (QVCCS)", "seconds": 724.0, "turns": 34}],
            "total_speaking_seconds": 724.0,
        },
        insights=[],
        related_links=[],
        transcript=Transcript(segments=[seg], language="en", duration_seconds=2.0),
        transcript_mode="foldout",
        enriched=True,
    )


def test_golden_note_end_to_end(tmp_path):
    cfg = MarkdownConfig(
        enabled=True,
        vault_path=str(tmp_path),
        route_by_client=True,
        filename_template="{date}_{slug}.md",
        transcript_mode="foldout",
    )
    path = MarkdownWriter(cfg).write_note(_ctx())
    got = path.read_text(encoding="utf-8")

    assert path.parent.name == "QVCCS Internal"
    markers = [
        "## Meeting overview",
        "## Executive summary",
        "## Discussion points",
        "## Decisions made",
        "> [!info]",
        "## Action items",
        "## Risks and blockers",
        "> [!warning]",
        "## Talk time",
        "## My Tasks",
        "- [ ] Rebuild callback logic #project/siemens-16 🔼 📅 2026-07-18",
        "> [!quote]- Full transcript",
    ]
    for marker in markers:
        assert marker in got, f"missing marker: {marker!r}\n---\n{got}"
    assert "—" not in got  # no em dash anywhere

    fm = yaml.safe_load(got.split("---\n")[1])
    assert fm["enriched"] is True
    assert isinstance(fm["attendees"], list) and isinstance(fm["tags"], list)
    assert fm["tags"] == ["qvccs-internal", "project/siemens-16"]
    assert fm["client"] == "QVCCS Internal"
    assert fm["source"] == "context-recall"

    # The My Tasks line the Meeting Action Items dashboard query matches on.
    assert "#project/siemens-16" in got


def test_golden_note_single_file(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True)
    MarkdownWriter(cfg).write_note(_ctx())
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1
