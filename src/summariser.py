"""
Meeting summarisation via Claude API or Ollama.

Takes a raw transcript and produces a structured summary containing:
- A concise title for the meeting
- High-level summary (2-3 paragraphs)
- Key decisions made
- Action items with assignees and deadlines (where detectable)
- Open questions or unresolved topics

The prompt is engineered to produce consistent, parseable Markdown
output that feeds directly into the Markdown and Notion writers.

Backend is configurable: set summarisation.backend to "claude" for the
Anthropic API, or "ollama" for a local Ollama model.
"""

import json
import logging
from dataclasses import dataclass, field

import anthropic
import httpx

from src.transcriber import Transcript
from src.utils.config import SummarisationConfig

logger = logging.getLogger(__name__)


SUMMARISATION_PROMPT = """\
You are a precise meeting summariser. Analyse the following transcript and produce a structured summary in Markdown.

Rules:
- Be concise in summaries but thorough on action items.
- The transcript may include speaker labels like [Me] and [Remote]. Use these to attribute statements, decisions, and action items to the correct speakers. "Me" is the person who recorded the meeting.
- If speaker names are identifiable from context, use them. Otherwise use the speaker labels provided.
- Action items are the MOST IMPORTANT section. Each must include: a clear task description, the full context of why it's needed, what was discussed that led to this task, any specific requirements or constraints mentioned, the owner, and the deadline.
- If the meeting is too short or incoherent to summarise meaningfully, say so briefly.

Output the summary in EXACTLY this format (no deviation):

# {Meeting Title}

## Summary
{2-3 paragraph summary of what was discussed and why it matters}

## Key Decisions
- {Decision 1}
- {Decision 2}

## Action Items

### {Action item 1 — short title}
- **Owner:** {Name} | **Deadline:** {Date or "TBD"}
- **Context:** {2-3 sentences explaining what was discussed that led to this task, why it matters, and any relevant background}
- **Requirements:** {Specific deliverables, constraints, or acceptance criteria mentioned in the meeting}
- [ ] {Concrete next step or subtask}
- [ ] {Additional subtask if applicable}

### {Action item 2 — short title}
- **Owner:** {Name} | **Deadline:** {Date or "TBD"}
- **Context:** {2-3 sentences explaining what was discussed that led to this task, why it matters, and any relevant background}
- **Requirements:** {Specific deliverables, constraints, or acceptance criteria mentioned in the meeting}
- [ ] {Concrete next step or subtask}
- [ ] {Additional subtask if applicable}

## Open Questions
- {Question or unresolved topic 1}
- {Question or unresolved topic 2}

## Tags
{Comma-separated list of 2-5 relevant topic tags, e.g. "project-x, roadmap, hiring"}
"""


@dataclass
class MeetingSummary:
    """Parsed output from the summariser."""

    raw_markdown: str = ""
    title: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_markdown(cls, markdown: str) -> "MeetingSummary":
        """
        Extract structured fields from the raw Markdown output.
        The title is taken from the first H1 heading. Tags are
        parsed from the ## Tags section.
        """
        title = ""
        tags = []

        for line in markdown.splitlines():
            stripped = line.strip()

            # Extract title from first H1.
            if stripped.startswith("# ") and not title:
                title = stripped[2:].strip()

            # Extract tags from the Tags section.
            if stripped.startswith("## Tags"):
                # The next non-empty line should contain comma-separated tags.
                idx = markdown.index(stripped) + len(stripped)
                rest = markdown[idx:].strip().split("\n")[0]
                tags = [t.strip() for t in rest.split(",") if t.strip()]

        return cls(
            raw_markdown=markdown,
            title=title or "Untitled Meeting",
            tags=tags,
        )


class Summariser:
    """
    Sends a meeting transcript to an LLM for structured summarisation.

    Supports two backends:
      - "claude": Anthropic Claude API (requires API key and credits)
      - "ollama": Local Ollama instance (free, runs on your machine)
    """

    def __init__(self, config: SummarisationConfig):
        self._config = config
        self._claude_client: anthropic.Anthropic | None = None

    def _get_claude_client(self) -> anthropic.Anthropic:
        """Lazy-initialise the Anthropic client."""
        if self._claude_client is None:
            if not self._config.anthropic_api_key:
                raise ValueError(
                    "Anthropic API key not set. Add it to config.yaml "
                    "under summarisation.anthropic_api_key, or switch to "
                    "backend: ollama."
                )
            self._claude_client = anthropic.Anthropic(
                api_key=self._config.anthropic_api_key
            )
        return self._claude_client

    def _prepare_transcript(self, transcript: Transcript) -> tuple[str, int]:
        """Prepare transcript text, applying truncation if needed."""
        text = transcript.timestamped_text
        word_count = transcript.word_count

        if word_count < 10:
            logger.warning(
                f"Transcript is very short ({word_count} words). "
                f"Summary may not be meaningful."
            )

        max_words = 50_000
        if word_count > max_words:
            logger.warning(
                f"Transcript exceeds {max_words} words ({word_count}). "
                f"Truncating to fit context window."
            )
            words = text.split()
            text = " ".join(words[:max_words]) + "\n\n[Transcript truncated]"

        return text, word_count

    def _build_user_message(
        self, transcript: Transcript, text: str, word_count: int
    ) -> str:
        """Build the user message content."""
        return (
            f"Here is the meeting transcript "
            f"({transcript.duration_seconds / 60:.0f} minutes, "
            f"{word_count} words):\n\n{text}"
        )

    def _summarise_claude(self, transcript: Transcript) -> MeetingSummary:
        """Summarise using the Anthropic Claude API."""
        text, word_count = self._prepare_transcript(transcript)

        logger.info(
            f"Sending {word_count}-word transcript to Claude "
            f"({self._config.model}) for summarisation..."
        )

        client = self._get_claude_client()
        message = client.messages.create(
            model=self._config.model,
            max_tokens=self._config.max_tokens,
            system=SUMMARISATION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": self._build_user_message(
                        transcript, text, word_count
                    ),
                }
            ],
        )

        return MeetingSummary.from_markdown(message.content[0].text)

    def _summarise_ollama(self, transcript: Transcript) -> MeetingSummary:
        """Summarise using a local Ollama instance."""
        text, word_count = self._prepare_transcript(transcript)
        model = self._config.ollama_model
        base_url = self._config.ollama_base_url.rstrip("/")

        logger.info(
            f"Sending {word_count}-word transcript to Ollama "
            f"({model}) for summarisation..."
        )

        user_content = self._build_user_message(transcript, text, word_count)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SUMMARISATION_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {
                "num_predict": self._config.max_tokens,
            },
        }

        response = httpx.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=300.0,  # Long timeout for large transcripts.
        )
        response.raise_for_status()

        data = response.json()
        raw_markdown = data["message"]["content"]

        return MeetingSummary.from_markdown(raw_markdown)

    def summarise(self, transcript: Transcript) -> MeetingSummary:
        """
        Generate a structured summary from a meeting transcript
        using the configured backend.
        """
        backend = self._config.backend.lower()

        if backend == "claude":
            summary = self._summarise_claude(transcript)
        elif backend == "ollama":
            summary = self._summarise_ollama(transcript)
        else:
            raise ValueError(
                f"Unknown summarisation backend: '{backend}'. "
                f"Use 'claude' or 'ollama'."
            )

        logger.info(
            f"Summary generated: '{summary.title}' "
            f"({len(summary.tags)} tags)"
        )
        return summary
