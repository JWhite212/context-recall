"""Tests for src/insights/extractor.py — parse_response (no LLM calls)."""

import pytest

from src.insights.extractor import InsightExtractor
from src.utils.config import SummarisationConfig

_DEF = {"id": "d1", "name": "Risks", "prompt": "List risks."}


@pytest.fixture
def extractor():
    return InsightExtractor(SummarisationConfig(backend="ollama"))


def test_parses_json_list(extractor):
    items = extractor.parse_response(
        '[{"content": "vendor lock-in", "speaker": "Me"}, {"content": "timeline slip"}]',
        _DEF,
    )
    assert [i["content"] for i in items] == ["vendor lock-in", "timeline slip"]
    assert items[0]["definition_id"] == "d1"
    assert items[0]["definition_name"] == "Risks"
    assert items[0]["speaker"] == "Me"
    assert items[1]["speaker"] == ""


def test_parses_markdown_fenced(extractor):
    items = extractor.parse_response('```json\n[{"content": "a"}]\n```', _DEF)
    assert len(items) == 1
    assert items[0]["content"] == "a"


def test_malformed_returns_empty(extractor):
    assert extractor.parse_response("not json at all", _DEF) == []


def test_empty_returns_empty(extractor):
    assert extractor.parse_response("", _DEF) == []


def test_drops_items_without_content(extractor):
    items = extractor.parse_response('[{"speaker": "Me"}, {"content": "ok"}]', _DEF)
    assert [i["content"] for i in items] == ["ok"]
