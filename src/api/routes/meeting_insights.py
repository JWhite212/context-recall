"""
Per-meeting insight endpoints.

GET  /api/meetings/{id}/talk-stats  — speaker talk-time breakdown
POST /api/meetings/{id}/draft-email — LLM follow-up email draft
"""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.summariser import Summariser
from src.talk_stats import compute_talk_stats
from src.utils.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger("contextrecall.api.meeting_insights")

router = APIRouter()

_repo = None
_ai_repo = None

EMAIL_PROMPT = """You draft follow-up emails after meetings. Given the meeting summary,
action items, and attendees, write a concise professional follow-up
email from the user's perspective.

Return ONLY a JSON object:
- "subject": a specific subject line (mention the meeting topic)
- "body": the email body in plain text — brief recap (2-3 sentences),
  then a bullet list of action items with owners, then a short sign-off.

Match the meeting's language. No markdown headers in the body."""


def init(repo, ai_repo=None) -> None:
    global _repo, _ai_repo
    _repo = repo
    _ai_repo = ai_repo


class DraftEmailRequest(BaseModel):
    instructions: str = Field(default="", max_length=1000)


@router.get("/api/meetings/{meeting_id}/talk-stats")
async def talk_stats(meeting_id: str):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return compute_talk_stats(meeting.transcript_json)


@router.post("/api/meetings/{meeting_id}/draft-email")
async def draft_email(meeting_id: str, body: DraftEmailRequest):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if not meeting.summary_markdown:
        raise HTTPException(status_code=400, detail="Meeting has no summary to draft from")

    action_items = []
    if _ai_repo is not None:
        try:
            action_items = await _ai_repo.list_by_meeting(meeting_id)
        except Exception:
            logger.warning("Could not load action items for draft", exc_info=True)

    try:
        attendees = json.loads(meeting.attendees_json or "[]")
    except (ValueError, TypeError):
        attendees = []
    attendee_names = ", ".join(a.get("name", "") for a in attendees if a.get("name"))

    items_text = (
        "\n".join(
            f"- {item['title']}"
            + (f" (owner: {item['assignee']})" if item.get("assignee") else "")
            + (f" (due: {item['due_date']})" if item.get("due_date") else "")
            for item in action_items
            if item.get("status") != "cancelled"
        )
        or "None recorded."
    )

    fence = "=" * 40
    user_msg = (
        f"Meeting: {meeting.title}\n"
        f"Attendees: {attendee_names or 'unknown'}\n"
        + (f"Extra instructions: {body.instructions}\n" if body.instructions else "")
        + f"\nAction items:\n{items_text}\n\n"
        f"{fence} BEGIN MEETING SUMMARY {fence}\n"
        f"{meeting.summary_markdown}\n"
        f"{fence} END MEETING SUMMARY {fence}"
    )

    config = load_config(DEFAULT_CONFIG_PATH)
    summariser = Summariser(config.summarisation)
    try:
        # Blocking LLM HTTP call — keep it off the event loop.
        response = await asyncio.to_thread(summariser.chat, EMAIL_PROMPT, user_msg)
    except Exception as e:
        logger.error("Email draft failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Draft generation failed — check the summarisation backend and daemon logs.",
        )

    subject, email_body = _parse_email(response, meeting.title)
    return {"subject": subject, "body": email_body}


def _parse_email(response: str, fallback_title: str) -> tuple[str, str]:
    """Parse the LLM's JSON; degrade to raw text if it ignored the format."""
    import re

    cleaned = (response or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and data.get("body"):
            return (
                str(data.get("subject") or f"Follow-up: {fallback_title}"),
                str(data["body"]),
            )
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, dict) and data.get("body"):
                    return (
                        str(data.get("subject") or f"Follow-up: {fallback_title}"),
                        str(data["body"]),
                    )
            except json.JSONDecodeError:
                pass
    return f"Follow-up: {fallback_title}", cleaned
