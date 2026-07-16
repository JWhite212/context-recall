"""
Meeting export endpoint.

POST /api/export/{id} — export a meeting as markdown or JSON.
"""

import json
import logging
import re
import time

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from src.output.markdown_writer import render_insights_section

logger = logging.getLogger("contextrecall.api.export")

router = APIRouter()

_repo = None
_insight_repo = None


def init(repo, insight_repo=None) -> None:
    """Inject the meeting repository and (optionally) the insight repository.

    ``insight_repo`` is optional and defaults to ``None`` so existing callers
    that only pass ``repo`` keep working; the Insights section is simply
    omitted from the export when it isn't wired up.
    """
    global _repo, _insight_repo
    _repo = repo
    _insight_repo = insight_repo


@router.post("/api/export/{meeting_id}", summary="Export meeting as Markdown or JSON")
async def export_meeting(
    meeting_id: str,
    format: str = Query("markdown", pattern="^(markdown|json)$"),
):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if format == "json":
        return JSONResponse(content=meeting.to_dict())

    # Markdown export.
    parts = []

    # YAML frontmatter.
    date_str = time.strftime("%Y-%m-%d", time.localtime(meeting.started_at))
    time_str = time.strftime("%H:%M", time.localtime(meeting.started_at))
    duration_min = int((meeting.duration_seconds or 0) / 60)

    safe_title = yaml.dump(meeting.title, default_flow_style=True, allow_unicode=True).strip()
    safe_tags = yaml.dump(meeting.tags, default_flow_style=True, allow_unicode=True).strip()
    parts.append("---")
    parts.append(f"title: {safe_title}")
    parts.append(f"date: {date_str}")
    parts.append(f"time: {time_str}")
    parts.append(f"duration_minutes: {duration_min}")
    parts.append(f"tags: {safe_tags}")
    parts.append("type: meeting-note")
    parts.append("---")
    parts.append("")

    # Summary.
    if meeting.summary_markdown:
        parts.append(meeting.summary_markdown)
        parts.append("")

    # Transcript.
    if meeting.transcript_json:
        try:
            segments = json.loads(meeting.transcript_json)
            parts.append("---")
            parts.append("")
            parts.append("## Full Transcript")
            parts.append("")
            parts.append("```")
            for seg in segments:
                ts = seg.get("start", 0)
                h, rem = divmod(int(ts), 3600)
                m, s = divmod(rem, 60)
                stamp = f"[{h:02d}:{m:02d}:{s:02d}]"
                speaker = seg.get("speaker", "")
                text = seg.get("text", "").strip()
                if speaker:
                    parts.append(f"{stamp} [{speaker}] {text}")
                else:
                    parts.append(f"{stamp} {text}")
            parts.append("```")
        except json.JSONDecodeError:
            pass

    # Insights — fetched live from the DB, since they're extracted after
    # the meeting's Obsidian note is already written (see MarkdownWriter).
    if _insight_repo is not None:
        results = await _insight_repo.results_for_meeting(meeting_id)
        insights_section = render_insights_section(results)
        if insights_section:
            parts.append("")
            parts.append(insights_section)

    content = "\n".join(parts)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", meeting_id)
    return PlainTextResponse(
        content=content,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_id}.md"',
        },
    )
