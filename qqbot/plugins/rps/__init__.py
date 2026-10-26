"""Native rock-paper-scissors command."""
import logging
import random
from dataclasses import dataclass
from typing import Any

from nonebot import get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel, Field

from infrastructure.onebot import at_segment, text_segment
from plugins.core import command_context_after_real_mention

BAN_SECONDS = 60
RPS_CHOICES = ("石头", "剪刀", "布")
RPS_BEATS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
LOGGER = logging.getLogger("qq-bot")

__plugin_meta__ = PluginMetadata(
    name="猜拳",
    description="与群成员猜拳，输家按配置时长禁言",
    usage="@bot /猜拳 @成员",
)


class Config(BaseModel):
    rps_ban_seconds: int = Field(default=BAN_SECONDS, ge=1, le=86400)


@dataclass(frozen=True)
class RpsResult:
    challenger_choice: str
    target_choice: str
    loser_id: str | None
    winner_id: str | None


def play_rock_paper_scissors(challenger_id: str, target_id: str) -> RpsResult:
    challenger_choice = random.choice(RPS_CHOICES)
    target_choice = random.choice(RPS_CHOICES)
    if challenger_choice == target_choice:
        return RpsResult(challenger_choice, target_choice, None, None)
    if RPS_BEATS[challenger_choice] == target_choice:
        return RpsResult(challenger_choice, target_choice, target_id, challenger_id)
    return RpsResult(challenger_choice, target_choice, challenger_id, target_id)


def format_rps_result(challenger_id: str, target_id: str, result: RpsResult,
                      ban_seconds: int = BAN_SECONDS,
                      ban_failure: str | None = None) -> list[dict[str, Any]]:
    segments = [text_segment("猜拳结果：\n发起者："), at_segment(challenger_id),
                text_segment(f"：{result.challenger_choice}\n挑战对象："), at_segment(target_id),
                text_segment(f"：{result.target_choice}\n")]
    if result.loser_id is None:
        segments.append(text_segment("平局！这次没人被禁言。"))
    else:
        segments.extend([text_segment("输家："), at_segment(result.loser_id)])
        if ban_failure:
            segments.append(text_segment(f"，{ban_failure}"))
        else:
            duration = f"{ban_seconds // 60}分钟" if ban_seconds % 60 == 0 else f"{ban_seconds}秒"
            segments.append(text_segment(f"，禁言{duration}。"))
    return segments


def describe_ban_failure(error: ActionFailed) -> str:
    info = error.info if isinstance(error.info, dict) else {}
    detail = " ".join(str(info.get(key, "")) for key in ("message", "wording")).lower()
    if "owner" in detail or "群主" in detail:
        return "群主无法被禁言，本局不执行禁言。"
    if "admin" in detail or "管理员" in detail:
        return "机器人无权禁言管理员，本局不执行禁言。"
    return "禁言失败，请确认机器人具有群管理权限。"


def _native_message(segments: list[dict[str, Any]]) -> Message:
    message = Message()
    for segment in segments:
        message += (MessageSegment.text(str(segment["data"]["text"])) if segment["type"] == "text"
                    else MessageSegment.at(str(segment["data"]["qq"])))
    return message


async def rps_command(event: MessageEvent) -> bool:
    context = command_context_after_real_mention(event)
    return bool(context and context.text.split(maxsplit=1)[0] == "/猜拳")


rps = on_message(rule=Rule(rps_command), priority=10, block=True)


@rps.handle()
async def handle_rps(bot: Bot, event: MessageEvent) -> None:
    context = command_context_after_real_mention(event)
    if not isinstance(event, GroupMessageEvent):
        await rps.finish("猜拳只能在群聊中使用。")
    if context is None or not context.mentions:
        await rps.finish("用法：@bot /猜拳 @成员")

    challenger_id = str(event.user_id)
    target_id = context.mentions[0]
    if target_id == challenger_id:
        await rps.finish("不能和自己猜拳。")
    if target_id == str(event.self_id):
        await rps.finish("不能和机器人猜拳。")

    ban_seconds = get_plugin_config(Config).rps_ban_seconds
    result = play_rock_paper_scissors(challenger_id, target_id)
    ban_failure = None
    if result.loser_id is not None:
        try:
            await bot.call_api("set_group_ban", group_id=event.group_id,
                               user_id=int(result.loser_id), duration=ban_seconds)
        except ActionFailed as error:
            ban_failure = describe_ban_failure(error)
            LOGGER.warning("RPS ban was rejected by OneBot: %s", error)
    await rps.finish(_native_message(format_rps_result(
        challenger_id,
        target_id,
        result,
        ban_seconds,
        ban_failure,
    )))
