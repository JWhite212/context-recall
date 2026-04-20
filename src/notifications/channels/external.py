"""External notification channels: webhook and email."""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from src.utils.config import EmailChannelConfig, WebhookChannelConfig

logger = logging.getLogger("meetingmind.notifications.external")


async def send_webhook(
    config: WebhookChannelConfig,
    title: str,
    body: str,
    type: str,
) -> bool:
    """POST a JSON payload to the configured webhook URL.

    Supports ``"slack"`` format (``{"text": ...}``) and ``"generic"``
    format (``{"title": ..., "body": ..., "type": ...}``).
    Returns True on success.
    """
    if not config.url:
        logger.warning("Webhook URL not configured; skipping")
        return False

    if config.format == "slack":
        payload = {"text": f"*{title}*\n{body}"}
    else:
        payload = {"title": title, "body": body, "type": type}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(config.url, json=payload)
            resp.raise_for_status()
        logger.debug("Webhook sent to %s (status %s)", config.url, resp.status_code)
        return True
    except Exception as e:
        logger.warning("Webhook delivery failed: %s", e)
        return False


async def send_email(
    config: EmailChannelConfig,
    title: str,
    body: str,
) -> bool:
    """Send an email notification via SMTP with TLS. Returns True on success."""
    if not config.smtp_host or not config.to_address:
        logger.warning("Email not fully configured; skipping")
        return False

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send_email_sync, config, title, body)
        logger.debug("Email sent to %s", config.to_address)
        return True
    except Exception as e:
        logger.warning("Email delivery failed: %s", e)
        return False


def _send_email_sync(config: EmailChannelConfig, title: str, body: str) -> None:
    """Blocking SMTP send — runs in executor."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[MeetingMind] {title}"
    msg["From"] = config.from_address or config.smtp_user
    msg["To"] = config.to_address
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as server:
        server.starttls()
        if config.smtp_user and config.smtp_password:
            server.login(config.smtp_user, config.smtp_password)
        server.send_message(msg)
