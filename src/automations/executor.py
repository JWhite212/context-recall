"""Runs the actions of a matched automation rule, reusing existing primitives."""

import asyncio
import json
import logging

import httpx

from src.automations.payload import build_circleback_payload, sign_payload
from src.insights.extractor import InsightExtractor
from src.notifications.channels.external import send_webhook
from src.notifications.channels.macos import send as macos_send
from src.transcriber import Transcript
from src.utils.config import WebhookChannelConfig

logger = logging.getLogger("contextrecall.automations.executor")


class ActionExecutor:
    """Executes apply_tag / run_insight / webhook / send_notes / notify actions for one meeting."""

    def __init__(self, repo, emit, services=None) -> None:
        self._repo = repo
        self._emit = emit
        self._services = services or {}

    async def run_rule(
        self, rule: dict, context: dict, meeting_id: str, *, run_side_effects: bool
    ) -> None:
        for action in rule.get("actions") or []:
            atype = action.get("type")
            try:
                if atype == "apply_tag":
                    await self._apply_tag(action, context, meeting_id)
                elif atype == "run_insight":
                    await self._run_insight(action, meeting_id)
                elif atype == "webhook" and run_side_effects:
                    await self._webhook(action, context, rule)
                elif atype == "send_notes" and run_side_effects:
                    await self._send_notes(action, meeting_id)
                elif atype == "notify" and run_side_effects:
                    await self._notify(action, context, rule, meeting_id)
            except Exception:
                logger.warning(
                    "Automation action %s failed for rule '%s'",
                    atype,
                    rule.get("name"),
                    exc_info=True,
                )

    async def _apply_tag(self, action: dict, context: dict, meeting_id: str) -> None:
        tags = list(context.get("tags") or [])
        changed = False
        for tag in action.get("tags") or []:
            if tag and tag not in tags:
                tags.append(tag)
                changed = True
        if not changed:
            return
        context["tags"] = tags  # accumulate for later rules in the same run
        await self._repo.update_meeting(meeting_id, tags=tags)

    async def _run_insight(self, action: dict, meeting_id: str) -> None:
        definition_id = action.get("definition_id")
        irepo = self._services.get("insight_repo")
        meeting = self._services.get("meeting")
        cfg = self._services.get("summarisation_config")
        if not (definition_id and irepo and meeting and cfg):
            return
        definition = await irepo.get(definition_id)
        if not definition or not definition.get("enabled", True):
            return
        transcript = Transcript.from_dict(
            json.loads(getattr(meeting, "transcript_json", None) or "{}")
        )
        extractor = InsightExtractor(cfg)
        results = await asyncio.to_thread(extractor.extract, transcript, [definition])
        await irepo.replace_results_for_definition(meeting_id, definition_id, results)

    async def _send_notes(self, action: dict, meeting_id: str) -> None:
        url = action.get("url")
        if not url:
            return
        meeting = self._services.get("meeting")
        irepo = self._services.get("insight_repo")
        airepo = self._services.get("action_items_repo")
        action_items = await airepo.list_by_meeting(meeting_id) if airepo else []
        insights = await irepo.results_for_meeting(meeting_id) if irepo else []
        payload = build_circleback_payload(
            meeting,
            action_items,
            insights,
            include_transcript=bool(action.get("include_transcript")),
        )
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = action.get("secret") or ""
        if secret:
            headers["x-signature"] = sign_payload(body, secret)
        await self._post_json(url, content=body, headers=headers)

    async def _post_json(self, url: str, *, content: bytes = None, headers: dict = None) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, content=content, headers=headers)
                resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("send_notes delivery failed: %s", e)
            return False

    async def _webhook(self, action: dict, context: dict, rule: dict) -> None:
        cfg = WebhookChannelConfig(
            enabled=True,
            url=action.get("url", ""),
            format=action.get("format", "generic"),
        )
        title = context.get("title") or "Context Recall"
        body = f"Automation '{rule.get('name')}' matched."
        await send_webhook(cfg, title, body, "automation")

    async def _notify(self, action: dict, context: dict, rule: dict, meeting_id: str) -> None:
        title = context.get("title") or "Context Recall"
        body = action.get("message") or f"Automation '{rule.get('name')}' matched."
        self._emit("notification", title=title, body=body, meeting_id=meeting_id)
        await macos_send(title, body)
