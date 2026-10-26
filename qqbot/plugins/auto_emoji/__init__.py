"""Native passive automatic emoji reactions."""
import logging
import re

from nonebot import get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel

LOGGER = logging.getLogger("qq-bot")
DEFAULT_CANDY_EMOJI_ID = "127852"
DEFAULT_BLESSING_EMOJI_ID = "12951"

__plugin_meta__ = PluginMetadata(
    name="自动贴表情",
    description="指定成员在群聊发言时自动贴配置的表情",
    usage="被动监听，无需 @bot",
)


class Config(BaseModel):
    auto_emoji_target_qq: str = ""
    auto_emoji_ids: str = f"{DEFAULT_CANDY_EMOJI_ID},{DEFAULT_BLESSING_EMOJI_ID}"


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


async def target_group_message(event: GroupMessageEvent) -> bool:
    config = get_plugin_config(Config)
    return bool(config.auto_emoji_target_qq and event.message_id
                and str(event.user_id) == config.auto_emoji_target_qq)


auto_emoji = on_message(rule=Rule(target_group_message), priority=5, block=False)


@auto_emoji.handle()
async def handle_auto_emoji(bot: Bot, event: GroupMessageEvent) -> None:
    for emoji_id in parse_emoji_ids(get_plugin_config(Config).auto_emoji_ids):
        try:
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id=emoji_id)
        except Exception:
            LOGGER.exception("Failed to set automatic emoji like %s for message %s", emoji_id, event.message_id)
