"""
Meeting summarisation via the Anthropic Claude API.

Takes a raw transcript and produces a structured summary containing:
- A concise title for the meeting
- High-level summary (2-3 paragraphs)
- Key decisions made
- Action items with assignees and deadlines (where detectable)
- Open questions or unresolved topics

The prompt is engineered to produce consistent, parseable Markdown
output that feeds directly into the Markdown and Notion writers.
"""

import json
import logging
from dataclasses import dataclass, field

import anthropic

from src.transcriber import Transcript
from src.utils.config import SummarisationConfig

logger = logging.getLogger(__name__)


SUMMARISATION_PROMPT = """\
You are a precise meeting summariser. Analyse the following transcript and produce a structured summary in Markdown.

Rules:
- Be concise. Capture substance, not filler.
- If speaker names are identifiable from context, use them. Otherwise use generic labels.
- Action items MUST include an owner (or "Unassigned") and a deadline (or "No deadline stated").
- If the meeting is too short or incoherent to summarise meaningfully, say so briefly.

Output the summary in EXACTLY this format (no deviation):

# {Meeting Title}

## Summary
{2-3 paragraph summary of what was discussed and why it matters}

## Key Decisions
- {Decision 1}
- {Decision 2}

## Action Items
- [ ] {Task description} — **Owner:** {Name} | **Deadline:** {Date or "TBD"}
- [ ] {Task description} — **Owner:** {Name} | **Deadline:** {Date or "TBD"}

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
    Sends a meeting transcript to Claude for structured summarisation.
    """

    def __init__(self, config: SummarisationConfig):
        self._config = config
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        """Lazy-initialise the Anthropic client."""
        if self._client is None:
            if not self._config.anthropic_api_key:
                raise ValueError(
                    "Anthropic API key not set. Add it to config.yaml "
                    "under summarisation.anthropic_api_key."
                )
            self._client = anthropic.Anthropic(
                api_key=self._config.anthropic_api_key
            )
        return self._client

    def summarise(self, transcript: Transcript) -> MeetingSummary:
        """
        Generate a structured summary from a meeting transcript.

        If the transcript is extremely long (>50,000 words), it is
        truncated with a note to the model. Claude's context window
        can handle long transcripts, but very long meetings may
        benefit from chunked summarisation in future iterations.
        """
        text = transcript.timestamped_text
        word_count = transcript.word_count

        if word_count < 10:
            logger.warning(
                f"Transcript is very short ({word_count} words). "
                f"Summary may not be meaningful."
            )

        # Truncation guard for exceptionally long meetings.
        max_words = 50_000
        if word_count > max_words:
            logger.warning(
                f"Transcript exceeds {max_words} words ({word_count}). "
                f"Truncating to fit context window."
            )
            words = text.split()
            text = " ".join(words[:max_words]) + "\n\n[Transcript truncated]"

        logger.info(
            f"Sending {word_count}-word transcript to Claude "
            f"({self._config.model}) for summarisation..."
        )

        client = self._get_client()

        message = client.messages.create(
            model=self._config.model,
            max_tokens=self._config.max_tokens,
            system=SUMMARISATION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Here is the meeting transcript "
                        f"({transcript.duration_seconds / 60:.0f} minutes, "
                        f"{word_count} words):\n\n{text}"
                    ),
                }
            ],
        )

        raw_markdown = message.content[0].text
        summary = MeetingSummary.from_markdown(raw_markdown)

        logger.info(f"Summary generated: '{summary.title}' ({len(summary.tags)} tags)")
        return summary
