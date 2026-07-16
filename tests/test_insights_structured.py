from src.insights.extractor import InsightExtractor, coerce_value, render_content


def test_coerce_number_date_boolean_list():
    assert coerce_value("42", "number") == 42
    assert coerce_value(3.5, "number") == 3.5
    assert coerce_value("not a num", "number") is None
    assert coerce_value("2026-09-02", "date") == "2026-09-02"
    assert coerce_value("02/09/2026", "date") is None  # only ISO accepted
    assert coerce_value("yes", "boolean") is True
    assert coerce_value(False, "boolean") is False
    assert coerce_value("Findings", "list") == ["Findings"]
    assert coerce_value(["a", "b"], "list") == ["a", "b"]
    assert coerce_value("plain", "text") == "plain"
    assert coerce_value(None, "text") is None


def test_render_content_joins_labels():
    fields = [
        {"key": "go_live", "label": "Go-live", "type": "date"},
        {"key": "blockers", "label": "Blockers", "type": "list"},
    ]
    record = {"go_live": "2026-09-02", "blockers": ["A", "B"]}
    out = render_content(record, fields)
    assert "Go-live: 2026-09-02" in out
    assert "Blockers: A; B" in out


def test_parse_structured_coerces_and_builds_one_item():
    fields = [
        {"key": "count", "label": "Count", "type": "number"},
        {"key": "items", "label": "Items", "type": "list"},
    ]
    definition = {"id": "d", "name": "Snapshot", "output_mode": "structured", "fields": fields}
    ext = InsightExtractor.__new__(InsightExtractor)  # no LLM init needed for parse
    out = ext.parse_structured('{"count": "5", "items": ["x", "y"]}', definition)
    assert len(out) == 1
    assert out[0]["fields"] == {"count": 5, "items": ["x", "y"]}
    assert out[0]["definition_id"] == "d"
    assert "Count: 5" in out[0]["content"]


def test_parse_structured_missing_field_is_null():
    fields = [{"key": "owner", "label": "Owner", "type": "text"}]
    definition = {"id": "d", "name": "X", "output_mode": "structured", "fields": fields}
    ext = InsightExtractor.__new__(InsightExtractor)
    out = ext.parse_structured("{}", definition)
    assert out[0]["fields"] == {"owner": None}


def test_parse_structured_malformed_json_returns_empty():
    definition = {
        "id": "d",
        "name": "X",
        "output_mode": "structured",
        "fields": [{"key": "a", "label": "A", "type": "text"}],
    }
    ext = InsightExtractor.__new__(InsightExtractor)
    assert ext.parse_structured("not json", definition) == []
