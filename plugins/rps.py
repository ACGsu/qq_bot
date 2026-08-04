import random
from dataclasses import dataclass
from typing import Any

from .common import CommandContext, at_segment, text_segment


BAN_SECONDS = 5 * 60
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


def format_rps_result(challenger_id: str, target_id: str, result: RpsResult) -> list[dict[str, Any]]:
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
                text_segment("，禁言5分钟。"),
            ]
        )
    return segments


class RockPaperScissorsPlugin:
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
            await bot._ban_group_member(websocket, str(event["group_id"]), result.loser_id, BAN_SECONDS)

        await bot._send_reply(websocket, event, format_rps_result(challenger_id, target_id, result))
