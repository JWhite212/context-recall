"""Tests for the LLM-backed per-meeting template selector."""

from unittest.mock import MagicMock

from src.template_selection import TemplateSelector
from src.templates import SummaryTemplate
from src.utils.config import SummarisationConfig

_TEMPLATES = [
    SummaryTemplate(name="standard", description="General meeting", system_prompt="x"),
    SummaryTemplate(name="discovery", description="Sales discovery call", system_prompt="y"),
]


def _selector(reply: str) -> TemplateSelector:
    sel = TemplateSelector(SummarisationConfig())
    sel._summariser = MagicMock()
    sel._summariser.chat.return_value = reply
    return sel


def test_selects_named_template():
    sel = _selector('{"template": "discovery", "confidence": 0.9}')
    assert (
        sel.select(
            "Acme discovery",
            [],
            "we want to explore your needs",
            _TEMPLATES,
            "standard",
            0.6,
        )
        == "discovery"
    )


def test_unknown_name_falls_back_to_default():
    sel = _selector('{"template": "nonsense", "confidence": 0.9}')
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_low_confidence_falls_back_to_default():
    sel = _selector('{"template": "discovery", "confidence": 0.2}')
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_fenced_json_is_parsed():
    sel = _selector('```json\n{"template": "discovery", "confidence": 0.8}\n```')
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "discovery"


def test_llm_exception_falls_back_to_default():
    sel = TemplateSelector(SummarisationConfig())
    sel._summariser = MagicMock()
    sel._summariser.chat.side_effect = RuntimeError("backend down")
    assert sel.select("x", [], "y", _TEMPLATES, "standard", 0.6) == "standard"


def test_fewer_than_two_templates_returns_default_without_calling_llm():
    sel = _selector('{"template": "discovery", "confidence": 0.9}')
    assert sel.select("x", [], "y", [_TEMPLATES[0]], "standard", 0.6) == "standard"
    sel._summariser.chat.assert_not_called()
