import random
import logging

from plugin_control import validate_ban_seconds
from dataclasses import dataclass
from typing import Any

from .common import CommandContext, at_segment, text_segment


BAN_SECONDS = 60
RPS_CHOICES = ("石头", "剪刀", "布")
RPS_BEATS = {
    "石头": "剪刀",
    "剪刀": "布",
    "布": "石头",
}


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


def format_rps_result(challenger_id: str, target_id: str, result: RpsResult, ban_seconds: int = BAN_SECONDS) -> list[dict[str, Any]]:
    segments = [
        text_segment("猜拳结果：\n发起者："),
        at_segment(challenger_id),
        text_segment(f"：{result.challenger_choice}\n挑战对象："),
        at_segment(target_id),
        text_segment(f"：{result.target_choice}\n"),
    ]
    if result.loser_id is None:
        segments.append(text_segment("平局！这次没人被禁言。"))
    else:
        segments.extend(
            [
                text_segment("输家："),
                at_segment(result.loser_id),
                text_segment(f"，禁言{ban_seconds // 60}分钟。" if ban_seconds % 60 == 0 else f"，禁言{ban_seconds}秒。"),
            ]
        )
    return segments


class RockPaperScissorsPlugin:
    def __init__(self, settings: dict[str, str] | None = None) -> None:
        try:
            self.ban_seconds = validate_ban_seconds((settings or {}).get("ban_seconds", "60"))
        except ValueError:
            self.ban_seconds = BAN_SECONDS
            logging.getLogger("qq-bot").warning("Invalid rps ban_seconds; using safe default 60 seconds")

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == "/猜拳"

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "猜拳只能在群聊中使用。")
            return
        if not context.mentions:
            await bot._send_reply(websocket, event, "用法：@bot /猜拳 @成员")
            return

        challenger_id = str(event.get("user_id"))
        target_id = context.mentions[0]
        if target_id == challenger_id:
            await bot._send_reply(websocket, event, "不能和自己猜拳。")
            return

        result = play_rock_paper_scissors(challenger_id, target_id)
        if result.loser_id is not None:
            await bot._ban_group_member(websocket, str(event["group_id"]), result.loser_id, self.ban_seconds)

        await bot._send_reply(websocket, event, format_rps_result(challenger_id, target_id, result, self.ban_seconds))
