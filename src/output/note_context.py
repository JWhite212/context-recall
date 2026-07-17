"""Structured input for the enriched Markdown note writer.

Pure data. The pipeline assembles a NoteContext from the meeting row and
the intelligence repositories; the writer and assembler consume it instead
of scraping the summary's raw markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.transcriber import Transcript


@dataclass
class ActionItemView:
    """One action item, flattened for rendering."""

    title: str
    assignee: str | None = None
    due_date: str | None = None  # ISO YYYY-MM-DD or None
    priority: str = "medium"  # low | medium | high | urgent
    status: str = "open"  # open | in_progress | done | cancelled
    description: str | None = None
    client_tag: str = ""  # "client/siemens" or ""
    project_tag: str = ""  # "project/siemens-16" or ""


@dataclass
class NoteContext:
    """Everything the writer needs to render one meeting note."""

    recall_id: str
    title: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    started_at: float
    duration_minutes: int
    word_count: int
    client_name: str = ""
    client_folder: str = "Unsorted"
    client_tag: str = ""  # "client/siemens" or ""
    project_name: str = ""
    project_tag: str = ""  # "project/siemens-16" or ""
    meeting_type: str = ""
    attendees: list[str] = field(default_factory=list)
    owner_display_name: str = "Jamie White (QVCCS)"
    extra_tags: list[str] = field(default_factory=list)
    summary_markdown: str = ""
    action_items: list[ActionItemView] = field(default_factory=list)
    owner_tasks: list[ActionItemView] = field(default_factory=list)
    insights: list[dict] = field(default_factory=list)
    talk_stats: dict = field(default_factory=dict)
    related_links: list[tuple[str, str]] = field(default_factory=list)  # (label, note_name)
    transcript: Transcript | None = None
    transcript_mode: str = "inline"
    enriched: bool = False

    @property
    def all_tags(self) -> list[str]:
        """Topic tags first, then the client and project tags, deduped.

        Empty entries are dropped so an unresolved client or project simply
        leaves its tag off rather than emitting a blank list item.
        """
        ordered = [*self.extra_tags, self.client_tag, self.project_tag]
        seen: set[str] = set()
        out: list[str] = []
        for tag in ordered:
            tag = (tag or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out
