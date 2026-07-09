"""LLM extraction of user-defined insights from meeting transcripts."""

import json
import logging
import re

from src.summariser import Summariser
from src.transcriber import Transcript
from src.utils.config import SummarisationConfig

logger = logging.getLogger("contextrecall.insights.extractor")

_SYSTEM_PROMPT = """You extract a specific kind of information from a meeting transcript.

The user wants: {instruction}

Return ONLY a JSON array. Each element is an object:
- "content": one concise item (a short phrase or sentence)
- "speaker": the speaker's name if attributable, else null

Return only genuine items. If there are none, return an empty array: []
No explanation, no markdown."""

_MAX_WORDS = 10000


class InsightExtractor:
    """Runs one LLM call per insight definition, returning list items."""

    def __init__(self, summarisation_config: SummarisationConfig) -> None:
        self._summariser = Summariser(summarisation_config)

    def extract(self, transcript: Transcript, definitions: list[dict]) -> list[dict]:
        text = transcript.full_text
        if not text or len(text.split()) < 10:
            return []
        words = text.split()
        if len(words) > _MAX_WORDS:
            text = " ".join(words[:5000]) + "\n...\n" + " ".join(words[-5000:])
        out: list[dict] = []
        for definition in definitions:
            try:
                response = self._call_llm(text, definition)
                out.extend(self.parse_response(response, definition))
            except Exception as e:
                logger.warning("Insight '%s' extraction failed: %s", definition.get("name"), e)
        return out

    def _call_llm(self, transcript_text: str, definition: dict) -> str:
        config = self._summariser._config
        system = _SYSTEM_PROMPT.format(instruction=definition["prompt"])
        fence = "=" * 40
        user_msg = (
            f"Insight: {definition['name']}\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n"
            f"{transcript_text}\n"
            f"{fence} END TRANSCRIPT {fence}"
        )
        if config.backend == "claude":
            return self._summariser._claude_chat(system, user_msg)
        base_url = Summariser._validate_ollama_url(config.ollama_base_url)
        return self._summariser._ollama_chat(base_url, config.ollama_model, system, user_msg)

    def parse_response(self, response: str, definition: dict) -> list[dict]:
        if not response:
            return []
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            items = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if not match:
                return []
            try:
                items = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            speaker = item.get("speaker")
            out.append(
                {
                    "definition_id": definition["id"],
                    "definition_name": definition["name"],
                    "content": content,
                    "speaker": str(speaker).strip() if speaker else "",
                }
            )
        return out
