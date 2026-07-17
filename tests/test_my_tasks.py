from src.output.note_assembler import format_my_task, render_my_tasks, select_owner_tasks
from src.output.note_context import ActionItemView


def test_line_format_medium_default_with_tags_and_date():
    item = ActionItemView(
        title="Rebuild the callback logic",
        priority="medium",
        due_date="2026-07-18",
        client_tag="client/siemens",
        project_tag="project/siemens-16",
    )
    line = format_my_task(item)
    assert line == (
        "- [ ] Rebuild the callback logic #client/siemens #project/siemens-16 🔼 📅 2026-07-18"
    )


def test_priority_emoji_mapping():
    assert "🔺" in format_my_task(ActionItemView(title="x", priority="urgent"))
    assert "⏫" in format_my_task(ActionItemView(title="x", priority="high"))
    assert "🔼" in format_my_task(ActionItemView(title="x", priority="medium"))
    low = format_my_task(ActionItemView(title="x", priority="low"))
    assert "🔽" in low and "🔼" not in low


def test_unknown_priority_defaults_to_medium_emoji():
    assert "🔼" in format_my_task(ActionItemView(title="x", priority="whatever"))


def test_no_date_when_missing():
    line = format_my_task(ActionItemView(title="x", priority="medium", project_tag="project/y"))
    assert "📅" not in line and line.endswith("🔼")


def test_render_my_tasks_matches_dashboard_query():
    items = [ActionItemView(title="Do X", project_tag="project/siemens-16")]
    section = render_my_tasks(items)
    assert section.startswith("## My Tasks")
    assert "- [ ] Do X" in section and "#project/siemens-16" in section


def test_render_empty_returns_blank():
    assert render_my_tasks([]) == ""


def test_select_owner_tasks_filters_incomplete_owner_items():
    items = [
        ActionItemView(title="mine open", assignee="Jamie", status="open"),
        ActionItemView(title="mine done", assignee="Me", status="done"),
        ActionItemView(title="theirs", assignee="Amelia", status="open"),
        ActionItemView(title="unassigned", assignee=None, status="open"),
    ]
    picked = select_owner_tasks(
        items, owner_identities=["me", "jamie"], owner_display_name="Jamie White (QVCCS)"
    )
    assert [i.title for i in picked] == ["mine open"]


def test_select_owner_tasks_matches_display_name():
    items = [ActionItemView(title="mine", assignee="Jamie White (QVCCS)", status="in_progress")]
    picked = select_owner_tasks(
        items, owner_identities=[], owner_display_name="Jamie White (QVCCS)"
    )
    assert [i.title for i in picked] == ["mine"]
