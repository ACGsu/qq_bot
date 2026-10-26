"""Native basic commands and configuration-aware help navigation."""
import asyncio
import logging
from typing import Any

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from infrastructure.runtime import runtime
from infrastructure.onebot import command_argument
from plugins.core import command_after_real_mention
from services.help import (
    HELP_LINES_BY_PLUGIN,
    MARRIAGE_HELP_LINES,
    build_help_card,
    format_help,
    format_help_topic,
)

LOGGER = logging.getLogger("qq-bot")
HELP_CARD_TIMEOUT_SECONDS = 30
HELP_CARD_FALLBACK_PREFIX = "卡片发送未确认，已切换为文字帮助。\n\n"

__plugin_meta__ = PluginMetadata(
    name="基础指令",
    description="提供分层帮助、文字帮助和 hello 测试回复",
    usage="@bot /help；@bot /hello",
)


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
    data = response.get("data") if isinstance(response, dict) and "data" in response else response
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if type(message_id) is int:
        return True
    if isinstance(message_id, str):
        digits = message_id.removeprefix("-")
        return digits.isascii() and digits.isdecimal()
    return False


async def basic_command(event: MessageEvent) -> bool:
    command = command_after_real_mention(event)
    return bool(command and command.split(maxsplit=1)[0] in {"/help", "/hello"})


basic = on_message(rule=Rule(basic_command), priority=10, block=True)


@basic.handle()
async def handle_basic(bot: Bot, event: MessageEvent) -> None:
    command = command_after_real_mention(event) or ""
    enabled = tuple(runtime.payload.get("enabled_plugins", ()))
    settings = runtime.payload.get("plugin_settings", {})
    reply = dispatch_command(command, enabled, settings)
    if reply is None:
        return

    if command.strip() == "/help" and isinstance(event, GroupMessageEvent):
        try:
            params = build_help_card(bot.self_id, enabled, settings)
            params["group_id"] = event.group_id
            response = await asyncio.wait_for(
                bot.call_api("send_group_forward_msg", **params),
                timeout=HELP_CARD_TIMEOUT_SECONDS,
            )
            if not _has_message_id(response):
                raise RuntimeError("help card result has no message_id")
            return
        except Exception as exc:
            LOGGER.warning("Help card send not confirmed (%s); falling back to text", type(exc).__name__)
            reply = HELP_CARD_FALLBACK_PREFIX + reply

    await basic.finish(reply)
