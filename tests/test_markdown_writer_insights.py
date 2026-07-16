"""Tests for render_insights_section in src/output/markdown_writer.py."""

from src.output.markdown_writer import render_insights_section


def test_render_insights_section_lists_and_structured():
    results = [
        {"definition_name": "Questions", "content": "Is it live?", "fields": None},
        {
            "definition_name": "Client Call Details",
            "content": "Go-live: 2026-09-02",
            "fields": {"go_live_date": "2026-09-02"},
        },
    ]
    md = render_insights_section(results)
    assert "## Insights" in md
    assert "Questions" in md
    assert "Is it live?" in md
    assert "Client Call Details" in md
    assert "2026-09-02" in md


def test_render_insights_section_empty_is_blank():
    assert render_insights_section([]) == ""
