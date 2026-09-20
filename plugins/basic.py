import logging
from typing import Any

from .common import CommandContext, command_argument
# Keep the existing text-help imports available to callers of plugins.basic.
from .help_menu import (
    HELP_LINES_BY_PLUGIN,
    MARRIAGE_HELP_LINES,
    build_help_card,
    format_help,
    format_help_topic,
)


LOGGER = logging.getLogger("qq-bot")
HELP_CARD_TIMEOUT_SECONDS = 30
HELP_CARD_FALLBACK_PREFIX = "卡片发送未确认，已切换为文字帮助。\n\n"


def dispatch_command(
    command: str,
    enabled_plugin_ids: tuple[str, ...] | None = None,
    plugin_settings: dict[str, dict[str, str]] | None = None,
) -> str | None:
    command_name = command.split(maxsplit=1)[0] if command else ""
    if command_name == "/help":
        return format_help_topic(command_argument(command), enabled_plugin_ids, plugin_settings)
    if command_name == "/hello":
        return "啦啦啦"
    return None


def _has_message_id(response: Any) -> bool:
    data = response.get("data") if isinstance(response, dict) else None
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if type(message_id) is int:
        return True
    if isinstance(message_id, str):
        digits = message_id.removeprefix("-")
        return digits.isascii() and digits.isdecimal()
    return False


class BasicCommandPlugin:
    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name in {"/help", "/hello"}

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        enabled_plugin_ids = getattr(bot, "enabled_plugin_ids", None)
        plugin_settings = getattr(bot, "plugin_settings", None)
        reply = dispatch_command(context.text, enabled_plugin_ids, plugin_settings)
        if reply is None:
            return

        if context.text.strip() == "/help" and event.get("message_type") == "group":
            try:
                bot_qq = event.get("self_id") or getattr(getattr(bot, "config", None), "bot_qq", None)
                params = build_help_card(bot_qq, enabled_plugin_ids, plugin_settings)
                params["group_id"] = str(event["group_id"])
                response = await bot._send_action_request(
                    websocket,
                    "send_group_forward_msg",
                    params,
                    timeout=HELP_CARD_TIMEOUT_SECONDS,
                )
                if not _has_message_id(response):
                    raise RuntimeError("Help card send returned no valid message_id")
                # QQ can render nested cards even when get_forward_msg reads back empty children.
                # The send acknowledgement is sufficient; do not fetch or resend the card.
                return
            except Exception as exc:
                LOGGER.warning("Help card send not confirmed (%s); falling back to text", type(exc).__name__)
                reply = HELP_CARD_FALLBACK_PREFIX + reply

        await bot._send_reply(websocket, event, reply)
