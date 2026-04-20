"""LLM-based action item extraction from meeting transcripts."""

import json
import logging
import re

from src.summariser import Summariser
from src.transcriber import Transcript
from src.utils.config import ActionItemsConfig, SummarisationConfig

logger = logging.getLogger("meetingmind.action_items.extractor")

EXTRACTION_PROMPT = """You are a precise action item extractor. Given a meeting transcript,
extract all action items, tasks, commitments, and follow-ups.

For each item return a JSON object with:
- "title": concise action in imperative form (e.g., "Draft Q2 roadmap proposal")
- "assignee": who is responsible (name or "unassigned" if unclear)
- "due_date": ISO date if mentioned (e.g., "2026-04-25") or null
- "priority": inferred from urgency language ("low", "medium", "high", "urgent")
- "extracted_text": the exact quote from the transcript that implies this action

Return ONLY a JSON array. No explanation, no markdown formatting.
Only include genuine commitments and assignments, not discussion points.
If there are no action items, return an empty array: []"""


class ActionItemExtractor:
    """Extracts structured action items from transcripts using LLM."""

    def __init__(self, summarisation_config: SummarisationConfig, config: ActionItemsConfig):
        self._summariser = Summariser(summarisation_config)
        self._config = config

    def extract(self, transcript: Transcript) -> list[dict]:
        """Extract action items from a transcript. Returns list of dicts."""
        text = transcript.full_text
        if not text or len(text.split()) < 10:
            return []
        # Truncate long transcripts
        words = text.split()
        if len(words) > 10000:
            text = " ".join(words[:5000]) + "\n...\n" + " ".join(words[-5000:])
        try:
            response = self._call_llm(text)
            return self.parse_response(response)
        except Exception as e:
            logger.warning("Action item extraction failed: %s", e)
            return []

    def _call_llm(self, transcript_text: str) -> str:
        """Call the configured LLM backend for extraction."""
        config = self._summariser._config
        fence = "=" * 40
        user_msg = (
            f"Extract action items from this meeting transcript.\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n"
            f"{transcript_text}\n"
            f"{fence} END TRANSCRIPT {fence}"
        )
        if config.backend == "claude":
            return self._summariser._claude_chat(EXTRACTION_PROMPT, user_msg)
        else:
            base_url = Summariser._validate_ollama_url(config.ollama_base_url)
            return self._summariser._ollama_chat(
                base_url, config.ollama_model, EXTRACTION_PROMPT, user_msg
            )

    def parse_response(self, response: str) -> list[dict]:
        """Parse LLM response into structured action items."""
        if not response:
            return []
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()
        try:
            items = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if match:
                try:
                    items = json.loads(match.group())
                except json.JSONDecodeError:
                    return []
            else:
                return []
        if not isinstance(items, list):
            return []
        valid_items = []
        for item in items:
            if not isinstance(item, dict) or "title" not in item:
                continue
            valid_items.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "assignee": str(item.get("assignee", "unassigned")).strip() or "unassigned",
                    "due_date": item.get("due_date"),
                    "priority": item.get("priority", "medium")
                    if item.get("priority") in ("low", "medium", "high", "urgent")
                    else "medium",
                    "extracted_text": str(item.get("extracted_text", "")).strip(),
                }
            )
        return valid_items
