"""Generate meeting prep briefings using LLM."""

import json
import logging

from src.action_items.repository import ActionItemRepository
from src.db.repository import MeetingRepository
from src.prep.repository import PrepRepository
from src.series.repository import SeriesRepository
from src.summariser import Summariser
from src.utils.config import PrepConfig, SummarisationConfig

logger = logging.getLogger("meetingmind.prep")

PREP_PROMPT = """You are preparing a concise meeting briefing. Generate a prep document with:
1. **Key Context** - what was discussed in previous meetings, decisions made
2. **Outstanding Items** - action items that should be followed up
3. **Suggested Talking Points** - based on open items and patterns
4. **People Context** - recent discussions with each attendee

Keep it under 500 words. Be direct and actionable. Use markdown formatting."""


class PrepBriefingGenerator:
    def __init__(
        self,
        config: PrepConfig,
        summarisation_config: SummarisationConfig,
        meeting_repo: MeetingRepository,
        action_item_repo: ActionItemRepository,
        series_repo: SeriesRepository,
        prep_repo: PrepRepository,
    ):
        self._config = config
        self._summariser = Summariser(summarisation_config)
        self._meeting_repo = meeting_repo
        self._action_item_repo = action_item_repo
        self._series_repo = series_repo
        self._prep_repo = prep_repo

    async def gather_context(self, attendees: list[str], series_id: str | None = None) -> dict:
        context = {"series_meetings": [], "attendee_meetings": [], "open_action_items": []}

        if series_id:
            meetings = await self._series_repo.get_meetings(series_id)
            context["series_meetings"] = meetings[: self._config.max_context_meetings]

        if attendees:
            rows = await self._meeting_repo.list_recent_complete_with_attendees(limit=100)
            for row in rows:
                try:
                    meeting_attendees = {
                        a.get("email", "").lower()
                        for a in json.loads(row["attendees_json"] or "[]")
                    }
                    if meeting_attendees & set(a.lower() for a in attendees):
                        context["attendee_meetings"].append(dict(row))
                        if len(context["attendee_meetings"]) >= self._config.max_attendee_history:
                            break
                except (json.JSONDecodeError, TypeError):
                    pass

        all_items = await self._action_item_repo.list_items(status="open", limit=50)
        all_items += await self._action_item_repo.list_items(status="in_progress", limit=50)
        context["open_action_items"] = all_items[:20]
        return context

    async def generate(
        self,
        title: str,
        attendees: list[str],
        attendee_names: list[str],
        series_id: str | None = None,
        meeting_id: str | None = None,
    ) -> str:
        context = await self.gather_context(attendees, series_id)
        user_msg = self._build_prompt(title, attendee_names, context)
        try:
            config = self._summariser._config
            if config.backend == "claude":
                content = self._summariser._claude_chat(PREP_PROMPT, user_msg)
            else:
                base_url = Summariser._validate_ollama_url(config.ollama_base_url)
                content = self._summariser._ollama_chat(
                    base_url, config.ollama_model, PREP_PROMPT, user_msg
                )
        except Exception as e:
            logger.warning("Prep briefing generation failed: %s", e)
            content = self._build_fallback(title, context)

        briefing_id = await self._prep_repo.create(
            content_markdown=content,
            attendees_json=json.dumps(attendee_names),
            series_id=series_id,
            meeting_id=meeting_id,
            related_meeting_ids_json=json.dumps([m["id"] for m in context["series_meetings"]]),
            open_action_items_json=json.dumps(
                [{"id": i["id"], "title": i["title"]} for i in context["open_action_items"][:10]]
            ),
        )
        return briefing_id

    def _build_prompt(self, title: str, attendees: list[str], context: dict) -> str:
        parts = [f"Meeting: {title}\nAttendees: {', '.join(attendees)}\n"]
        if context["series_meetings"]:
            parts.append("## Previous meetings in this series:")
            for m in context["series_meetings"][:3]:
                summary = (m.get("summary_markdown") or "")[:500]
                parts.append(f"- {m.get('title', 'Untitled')}: {summary}")
        if context["open_action_items"]:
            parts.append("\n## Open action items:")
            for item in context["open_action_items"][:10]:
                parts.append(
                    f"- [{item['status']}] {item['title']} (assigned: {item.get('assignee', '?')})"
                )
        if context["attendee_meetings"]:
            parts.append("\n## Recent meetings with these people:")
            for m in context["attendee_meetings"][:5]:
                parts.append(f"- {m.get('title', 'Untitled')}")
        return "\n".join(parts)

    def _build_fallback(self, title: str, context: dict) -> str:
        parts = [f"# Prep: {title}\n"]
        if context["open_action_items"]:
            parts.append("## Open Action Items")
            for item in context["open_action_items"][:5]:
                parts.append(f"- {item['title']} ({item.get('assignee', 'unassigned')})")
        if context["series_meetings"]:
            parts.append("\n## Previous Meetings")
            for m in context["series_meetings"][:3]:
                parts.append(f"- {m.get('title', 'Untitled')}")
        return "\n".join(parts)
