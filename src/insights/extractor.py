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

_STRUCTURED_SYSTEM_PROMPT = """You extract a structured record from a meeting transcript.

The user wants: {instruction}

Return ONLY a single JSON object with EXACTLY these keys:
{field_lines}

Rules:
- date fields: ISO format "YYYY-MM-DD" or null if not stated.
- number fields: a JSON number or null.
- boolean fields: true or false.
- list fields: a JSON array of short strings (empty array if none).
- text fields: a short string or null.
Use null when the transcript does not state a value. No explanation, no markdown."""

_MAX_WORDS = 10000


def coerce_value(value, type_):
    """Coerce a raw JSON value to the declared field type, or None if it doesn't fit."""
    if value is None:
        return None
    if type_ == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            f = float(str(value).strip())
            return int(f) if f.is_integer() else f
        except (ValueError, TypeError):
            return None
    if type_ == "date":
        s = str(value).strip()
        return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None
    if type_ == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "y", "1"}
    if type_ == "list":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        s = str(value).strip()
        return [s] if s else []
    # text
    s = str(value).strip()
    return s or None


def render_content(record, field_defs):
    """Render a coerced field record into a human-readable summary line."""
    parts = []
    for f in field_defs:
        v = record.get(f["key"])
        if v is None or v == [] or v == "":
            continue
        shown = "; ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        parts.append(f"{f['label']}: {shown}")
    return " · ".join(parts)


class InsightExtractor:
    """Runs one LLM call per insight definition, returning list or structured items."""

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
                if definition.get("output_mode") == "structured" and definition.get("fields"):
                    response = self._call_structured(text, definition)
                    out.extend(self.parse_structured(response, definition))
                else:
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

    def _call_structured(self, transcript_text: str, definition: dict) -> str:
        config = self._summariser._config
        field_lines = "\n".join(
            f'- "{f["key"]}" ({f["type"]}): {f["label"]}' for f in definition.get("fields") or []
        )
        system = _STRUCTURED_SYSTEM_PROMPT.format(
            instruction=definition["prompt"], field_lines=field_lines
        )
        fence = "=" * 40
        user_msg = (
            f"Insight: {definition['name']}\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n{transcript_text}\n{fence} END TRANSCRIPT {fence}"
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
                    "fields": None,
                }
            )
        return out

    def parse_structured(self, response: str, definition: dict) -> list[dict]:
        if not response:
            return []
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", response.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return []
            try:
                obj = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        if not isinstance(obj, dict):
            return []
        field_defs = definition.get("fields") or []
        record = {f["key"]: coerce_value(obj.get(f["key"]), f["type"]) for f in field_defs}
        return [
            {
                "definition_id": definition["id"],
                "definition_name": definition["name"],
                "content": render_content(record, field_defs),
                "speaker": "",
                "fields": record,
            }
        ]
