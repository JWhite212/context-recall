"""macOS native notification via osascript."""

import asyncio
import logging

logger = logging.getLogger("meetingmind.notifications.macos")


async def send(title: str, body: str, subtitle: str = "") -> None:
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_subtitle = subtitle.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    if safe_subtitle:
        script += f' subtitle "{safe_subtitle}"'
    script += ' sound name "default"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("osascript failed: %s", stderr.decode())
    except Exception as e:
        logger.warning("macOS notification failed: %s", e)
