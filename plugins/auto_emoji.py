import logging
import os
import re
from typing import Any

from .common import CommandContext


LOGGER = logging.getLogger("qq-bot")
DEFAULT_CANDY_EMOJI_ID = "127852"
DEFAULT_BLESSING_EMOJI_ID = "12951"


class AutoEmojiPlugin:
    plugin_id = "auto_emoji"

    def __init__(self, settings: dict[str, str] | None = None) -> None:
        settings = settings or {}
        self.target_qq = (
            settings.get("target_qq")
            or os.getenv("AUTO_CANDY_QQ")
            or os.getenv("AUTO_EMOJI_QQ")
            or ""
        ).strip()
        self.emoji_ids = parse_emoji_ids(
            settings.get("emoji_ids")
            or settings.get("emoji_id")
            or os.getenv("AUTO_EMOJI_IDS")
            or os.getenv("AUTO_CANDY_EMOJI_ID")
            or os.getenv("AUTO_EMOJI_ID")
            or f"{DEFAULT_CANDY_EMOJI_ID},{DEFAULT_BLESSING_EMOJI_ID}"
        )

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return False

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        return None

    async def handle_event(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        message_id = event.get("message_id")
        if not self.target_qq or event.get("message_type") != "group" or not message_id:
            return
        if str(event.get("user_id")) != self.target_qq:
            return

        for emoji_id in self.emoji_ids:
            try:
                await bot._send_action(
                    websocket,
                    "set_msg_emoji_like",
                    {
                        "message_id": int(message_id),
                        "emoji_id": emoji_id,
                    },
                )
            except Exception:
                LOGGER.exception("Failed to set automatic emoji like %s for message %s", emoji_id, message_id)


def parse_emoji_ids(raw: str) -> list[int | str]:
    emoji_ids: list[int | str] = []
    seen: set[str] = set()
    for item in re.split(r"[\s,\uFF0C;\uFF1B]+", raw):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        try:
            emoji_ids.append(int(value))
        except ValueError:
            emoji_ids.append(value)
    return emoji_ids
