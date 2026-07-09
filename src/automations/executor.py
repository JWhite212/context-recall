"""Runs the actions of a matched automation rule, reusing existing primitives."""

import logging

from src.notifications.channels.external import send_webhook
from src.notifications.channels.macos import send as macos_send
from src.utils.config import WebhookChannelConfig

logger = logging.getLogger("contextrecall.automations.executor")


class ActionExecutor:
    """Executes apply_tag / webhook / notify actions for one meeting."""

    def __init__(self, repo, emit) -> None:
        self._repo = repo
        self._emit = emit

    async def run_rule(
        self, rule: dict, context: dict, meeting_id: str, *, run_side_effects: bool
    ) -> None:
        for action in rule.get("actions") or []:
            atype = action.get("type")
            try:
                if atype == "apply_tag":
                    await self._apply_tag(action, context, meeting_id)
                elif atype == "webhook" and run_side_effects:
                    await self._webhook(action, context, rule)
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
