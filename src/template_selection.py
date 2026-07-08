"""LLM selection of the best summary template for a meeting.

Runs before summarisation, so it sees the title, attendees and transcript
(never the summary). Mirrors src/tagging/assigner.py's LlmAssigner: a
one-shot chat call, a fenced-JSON parse, and a graceful fallback to the
configured default template on any failure.
"""

from __future__ import annotations

import json
import logging
import re

from src.summariser import Summariser
from src.templates import SummaryTemplate
from src.utils.config import SummarisationConfig

logger = logging.getLogger(__name__)

TEMPLATE_SELECT_PROMPT = """You choose the most appropriate meeting-notes template.

Given a meeting's title, attendees and a transcript excerpt, plus a list of
available templates (name and description), pick the single best-fitting one.

Return ONLY a JSON object:
- "template": the name of the best template, exactly as given
- "confidence": 0.0-1.0, how certain you are
- "rationale": one short sentence

Rules:
- Pick only from the provided template names. Never invent a name.
- If none clearly fit, pick the most general one with low confidence."""

_TRANSCRIPT_EXCERPT_CHARS = 2000


class TemplateSelector:
    """Picks a template name for a meeting; falls back to a default."""

    def __init__(self, summarisation_config: SummarisationConfig) -> None:
        self._summariser = Summariser(summarisation_config)

    def select(
        self,
        title: str,
        attendees: list[dict],
        transcript_text: str,
        templates: list[SummaryTemplate],
        default_name: str,
        min_confidence: float,
    ) -> str:
        names = {t.name for t in templates}
        if len(names) < 2:
            return default_name
        try:
            response = self._call_llm(title, attendees, transcript_text, templates)
            picked = self._parse(response, names, min_confidence)
            return picked or default_name
        except Exception as e:  # never fail summarisation on selection
            logger.warning("Template selection failed: %s", e)
            return default_name

    def _call_llm(
        self,
        title: str,
        attendees: list[dict],
        transcript_text: str,
        templates: list[SummaryTemplate],
    ) -> str:
        template_lines = [f"- {t.name}: {t.description}" for t in templates]
        attendee_names = ", ".join(a.get("name", "") for a in attendees if a.get("name"))
        excerpt = transcript_text[:_TRANSCRIPT_EXCERPT_CHARS]
        fence = "=" * 40
        user_msg = (
            "Available templates:\n" + "\n".join(template_lines) + "\n\n"
            f"Meeting title: {title}\n"
            f"Attendees: {attendee_names or 'unknown'}\n\n"
            f"{fence} BEGIN TRANSCRIPT EXCERPT {fence}\n"
            f"{excerpt}\n"
            f"{fence} END TRANSCRIPT EXCERPT {fence}"
        )
        return self._summariser.chat(TEMPLATE_SELECT_PROMPT, user_msg)

    def _parse(self, response: str, names: set[str], min_confidence: float) -> str | None:
        if not response:
            return None
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        name = data.get("template")
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if name in names and confidence >= min_confidence:
            return name
        return None
