"""LLM-based speaker-name suggestions from transcript context.

People introduce themselves ("Hi, it's Sarah here", "This is Marcus
from finance") and address each other by name. This pass reads the
speaker-labelled transcript and proposes names for the labels the
diariser could not resolve. Suggestions are stored as ``candidate:``
speaker mappings — the same shape calendar attendees use — so the UI
offers them without silently renaming anyone.
"""

import json
import logging
import re

from src.summariser import Summariser
from src.transcriber import Transcript
from src.utils.config import SummarisationConfig
from src.voice.recognition import is_unresolved_label

logger = logging.getLogger("contextrecall.people.suggester")

SUGGESTION_PROMPT = """You are a precise meeting-transcript analyst. Given a transcript whose
lines are prefixed with speaker labels in [brackets], work out the real
names of the speakers with generic labels (like "Remote", "Remote 2",
or "SPEAKER_00").

Use only evidence inside the transcript: self-introductions, people
addressing each other by name, sign-offs. Do not guess from topic.

Return ONLY a JSON array. Each element:
- "speaker_label": the generic label exactly as it appears in brackets
- "suggested_name": the person's name as stated in the conversation
- "evidence": the exact quote that supports the identification

Only include suggestions with clear evidence. If there are none,
return an empty array: []"""

_MAX_WORDS = 6000


class SpeakerSuggester:
    """Suggests real names for unresolved speaker labels."""

    def __init__(self, summarisation_config: SummarisationConfig):
        self._summariser = Summariser(summarisation_config)

    def suggest(self, transcript: Transcript, remote_label: str = "Remote") -> list[dict]:
        """Return [{"speaker_label", "suggested_name", "evidence"}, ...]."""
        unresolved = {
            seg.speaker
            for seg in transcript.segments
            if seg.speaker and is_unresolved_label(seg.speaker, remote_label)
        }
        if not unresolved:
            return []

        text = transcript.timestamped_text
        words = text.split()
        if len(words) > _MAX_WORDS:
            # Introductions cluster at the start; sign-offs at the end.
            head = " ".join(words[: _MAX_WORDS // 2])
            tail = " ".join(words[-_MAX_WORDS // 2 :])
            text = f"{head}\n...\n{tail}"

        try:
            response = self._call_llm(text)
            return self._parse(response, unresolved)
        except Exception as e:
            logger.warning("Speaker suggestion failed: %s", e)
            return []

    def _call_llm(self, transcript_text: str) -> str:
        config = self._summariser._config
        fence = "=" * 40
        user_msg = (
            f"Identify the generically-labelled speakers in this transcript.\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n"
            f"{transcript_text}\n"
            f"{fence} END TRANSCRIPT {fence}"
        )
        if config.backend == "claude":
            return self._summariser._claude_chat(SUGGESTION_PROMPT, user_msg)
        base_url = Summariser._validate_ollama_url(config.ollama_base_url)
        return self._summariser._ollama_chat(
            base_url, config.ollama_model, SUGGESTION_PROMPT, user_msg
        )

    @staticmethod
    def _parse(response: str, unresolved: set[str]) -> list[dict]:
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
        valid = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("speaker_label", "")).strip()
            name = str(item.get("suggested_name", "")).strip()
            if not label or not name or label not in unresolved:
                continue
            valid.append(
                {
                    "speaker_label": label,
                    "suggested_name": name,
                    "evidence": str(item.get("evidence", "")).strip(),
                }
            )
        return valid
