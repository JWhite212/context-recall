from src.output.related import resolve_related


def test_previous_instance_from_series(tmp_path):
    prev = tmp_path / "Siemens" / "2026-07-08 - prev.md"
    prev.parent.mkdir(parents=True)
    prev.write_text("x")
    series = [
        {"id": "a", "started_at": 100.0, "markdown_path": str(prev)},
        {"id": "b", "started_at": 200.0, "markdown_path": ""},  # this meeting
    ]
    out = resolve_related(
        series_meetings=series,
        this_started_at=200.0,
        project_note_name="",
        vault_base=str(tmp_path),
        client_folder="Siemens",
    )
    assert ("Previous", "2026-07-08 - prev") in out


def test_most_recent_earlier_sibling_wins(tmp_path):
    older = tmp_path / "older.md"
    newer = tmp_path / "newer.md"
    older.write_text("x")
    newer.write_text("x")
    series = [
        {"id": "a", "started_at": 100.0, "markdown_path": str(older)},
        {"id": "b", "started_at": 150.0, "markdown_path": str(newer)},
        {"id": "c", "started_at": 200.0, "markdown_path": ""},
    ]
    out = resolve_related(
        series_meetings=series,
        this_started_at=200.0,
        project_note_name="",
        vault_base=str(tmp_path),
        client_folder="",
    )
    assert ("Previous", "newer") in out and ("Previous", "older") not in out


def test_project_link_found_in_sibling_projects_folder(tmp_path):
    # Real layout: vault_base is <root>/70 Meetings; project notes live in the
    # sibling <root>/10 Projects. The 'Project <name>' convention is matched.
    meetings = tmp_path / "70 Meetings"
    meetings.mkdir()
    (tmp_path / "10 Projects").mkdir()
    (tmp_path / "10 Projects" / "Project Siemens 16 Smart UK Infrastructure.md").write_text("x")
    out = resolve_related(
        series_meetings=[],
        this_started_at=0.0,
        project_note_name="Siemens 16 Smart UK Infrastructure",
        vault_base=str(meetings),
        client_folder="Siemens",
    )
    assert ("Project", "Project Siemens 16 Smart UK Infrastructure") in out


def test_project_link_exact_name_match(tmp_path):
    meetings = tmp_path / "70 Meetings"
    meetings.mkdir()
    (tmp_path / "10 Projects").mkdir()
    (tmp_path / "10 Projects" / "Armacell.md").write_text("x")
    out = resolve_related(
        series_meetings=[],
        this_started_at=0.0,
        project_note_name="Armacell",
        vault_base=str(meetings),
        client_folder="Armacell",
    )
    assert ("Project", "Armacell") in out


def test_missing_project_note_is_not_linked(tmp_path):
    meetings = tmp_path / "70 Meetings"
    meetings.mkdir()
    (tmp_path / "10 Projects").mkdir()
    out = resolve_related(
        series_meetings=[],
        this_started_at=0.0,
        project_note_name="Nonexistent",
        vault_base=str(meetings),
        client_folder="Siemens",
    )
    assert out == []


def test_no_earlier_sibling_returns_no_previous(tmp_path):
    out = resolve_related(
        series_meetings=[{"id": "b", "started_at": 200.0, "markdown_path": ""}],
        this_started_at=200.0,
        project_note_name="",
        vault_base=str(tmp_path),
        client_folder="Siemens",
    )
    assert out == []


def test_sibling_with_missing_file_is_skipped(tmp_path):
    series = [{"id": "a", "started_at": 100.0, "markdown_path": str(tmp_path / "gone.md")}]
    out = resolve_related(
        series_meetings=series,
        this_started_at=200.0,
        project_note_name="",
        vault_base=str(tmp_path),
        client_folder="",
    )
    assert out == []
