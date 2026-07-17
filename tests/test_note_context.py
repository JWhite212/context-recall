from src.output.note_context import ActionItemView, NoteContext


def test_all_tags_orders_extra_then_client_then_project_dropping_empties():
    ctx = NoteContext(
        recall_id="m1",
        title="T",
        date="2026-07-15",
        time="10:03",
        started_at=0.0,
        duration_minutes=28,
        word_count=4456,
        client_tag="client/siemens",
        project_tag="project/siemens-16",
        extra_tags=["qvccs-internal"],
    )
    assert ctx.all_tags == ["qvccs-internal", "client/siemens", "project/siemens-16"]


def test_all_tags_dedupes_and_skips_blank():
    ctx = NoteContext(
        recall_id="m1",
        title="T",
        date="2026-07-15",
        time="10:03",
        started_at=0.0,
        duration_minutes=1,
        word_count=1,
        client_tag="",
        project_tag="project/x",
        extra_tags=["project/x", "topic"],
    )
    assert ctx.all_tags == ["project/x", "topic"]


def test_defaults_are_safe():
    ctx = NoteContext(
        recall_id="m1",
        title="T",
        date="2026-07-15",
        time="10:03",
        started_at=0.0,
        duration_minutes=1,
        word_count=1,
    )
    assert ctx.attendees == [] and ctx.action_items == [] and ctx.enriched is False
    assert ctx.client_folder == "Unsorted" and ctx.transcript_mode == "inline"


def test_action_item_view_defaults():
    item = ActionItemView(title="Do X")
    assert item.priority == "medium" and item.status == "open"
    assert item.client_tag == "" and item.project_tag == ""
