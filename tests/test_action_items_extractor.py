"""Tests for ActionItemExtractor parse logic."""

import json

import pytest

from src.action_items.extractor import ActionItemExtractor
from src.utils.config import ActionItemsConfig, SummarisationConfig


@pytest.fixture
def extractor():
    sum_config = SummarisationConfig(backend="ollama")
    ai_config = ActionItemsConfig()
    return ActionItemExtractor(summarisation_config=sum_config, config=ai_config)


def test_parse_llm_response_valid(extractor):
    response = json.dumps(
        [
            {
                "title": "Draft proposal",
                "assignee": "Alice",
                "due_date": "2026-04-25",
                "priority": "high",
                "extracted_text": "can you draft the proposal by Friday",
            },
            {
                "title": "Review audit",
                "assignee": "Bob",
                "due_date": None,
                "priority": "medium",
                "extracted_text": "please review the security audit",
            },
        ]
    )
    items = extractor.parse_response(response)
    assert len(items) == 2
    assert items[0]["title"] == "Draft proposal"
    assert items[0]["assignee"] == "Alice"
    assert items[0]["priority"] == "high"
    assert items[1]["assignee"] == "Bob"


def test_parse_llm_response_malformed(extractor):
    items = extractor.parse_response("Not valid JSON at all")
    assert items == []


def test_parse_llm_response_with_markdown_fence(extractor):
    response = (
        '```json\n[{"title": "Test", "assignee": "Me", '
        '"due_date": null, "priority": "medium", '
        '"extracted_text": "test"}]\n```'
    )
    items = extractor.parse_response(response)
    assert len(items) == 1
    assert items[0]["title"] == "Test"


def test_parse_empty_response(extractor):
    assert extractor.parse_response("") == []
    assert extractor.parse_response("[]") == []


def test_parse_invalid_priority_defaults_to_medium(extractor):
    response = json.dumps(
        [{"title": "Task", "assignee": "Me", "priority": "critical", "extracted_text": "x"}]
    )
    items = extractor.parse_response(response)
    assert items[0]["priority"] == "medium"


def test_parse_skips_items_without_title(extractor):
    response = json.dumps(
        [{"assignee": "Me"}, {"title": "Valid", "assignee": "Me", "extracted_text": "y"}]
    )
    items = extractor.parse_response(response)
    assert len(items) == 1
    assert items[0]["title"] == "Valid"
