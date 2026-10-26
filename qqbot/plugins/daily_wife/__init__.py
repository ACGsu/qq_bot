"""Native daily-wife draw and optional marriage conversation."""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from nonebot import get_driver, get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel

from infrastructure.onebot import at_segment, qq_avatar_segment, text_segment, today_date_key
from plugins.core import command_context_after_real_mention

LOGGER = logging.getLogger("qq-bot")
DAILY_WIFE_COMMANDS = {"/今日群友", "/今日老婆"}
MARRIAGE_HINT = "\n结芬~：输入 @bot /结芬 发起结芬请求"
MARRIAGE_RESPONSE_SECONDS = 2 * 60
MARRIAGE_ACCEPT_COMMANDS = {"/愿意", "愿意", "/同意", "同意", "/接受", "接受"}
MARRIAGE_REJECT_COMMANDS = {"/不愿意", "不愿意", "/拒绝", "拒绝", "/不要", "不要"}
MARRIAGE_RESULT_ACCEPTED = "accepted"
MARRIAGE_RESULT_REJECTED = "rejected"
MARRIAGE_FAIL_MESSAGE = "雑魚~雑魚~，被甩了捏~"
MARRIAGE_SUCCESS_MESSAGE = "正在制作结婚证（其实是生图ai没钱弄）"

__plugin_meta__ = PluginMetadata(
    name="今日群友", description="每日抽取群友，并提供可配置的结芬回应流程",
    usage="@bot /今日群友；@bot /结芬；@bot /愿意 或 /不愿意",
)


class Config(BaseModel):
    daily_wife_marriage_enabled: bool = True


@dataclass(frozen=True)
class DailyWifeRecord:
    date_key: str
    target_id: str
    target_name: str


@dataclass
class MarriageProposal:
    group_id: str
    requester_id: str
    target_id: str
    target_name: str
    date_key: str
    created_at: float
    timeout_task: asyncio.Task[None] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class MarriageResultRecord:
    date_key: str
    target_id: str
    target_name: str
    result: str


daily_wives: dict[tuple[str, str], DailyWifeRecord] = {}
marriage_results: dict[tuple[str, str], MarriageResultRecord] = {}
marriage_proposals_by_requester: dict[tuple[str, str], MarriageProposal] = {}
marriage_proposals_by_target: dict[tuple[str, str], MarriageProposal] = {}


def member_display_name(member: dict[str, Any]) -> str:
    for key in ("card", "nickname", "title"):
        value = str(member.get(key) or "").strip()
        if value:
            return value
    return "这位群友"


def wife_candidates(members: list[dict[str, Any]], requester_id: str) -> list[dict[str, Any]]:
    return [member for member in members if str(member.get("user_id") or "") not in {"", requester_id}]


def format_daily_wife_new(record: DailyWifeRecord, marriage_enabled: bool = True) -> list[dict[str, Any]]:
    hint = MARRIAGE_HINT if marriage_enabled else ""
    return [qq_avatar_segment(record.target_id), text_segment("\n"), at_segment(record.target_id),
            text_segment(f" {record.target_name}是你的今日群友老婆~{hint}")]


def format_daily_wife_existing(record: DailyWifeRecord, marriage_enabled: bool = True) -> list[dict[str, Any]]:
    hint = MARRIAGE_HINT if marriage_enabled else ""
    return [text_segment("你已经抽取了今日的群友老婆："), at_segment(record.target_id),
            text_segment(f" {record.target_name}{hint}")]


def format_marriage_result(record: MarriageResultRecord) -> str:
    return MARRIAGE_SUCCESS_MESSAGE if record.result == MARRIAGE_RESULT_ACCEPTED else MARRIAGE_FAIL_MESSAGE


def _native_message(segments: list[dict[str, Any]]) -> Message:
    message = Message()
    for segment in segments:
        if segment["type"] == "text":
            message += MessageSegment.text(str(segment["data"].get("text", "")))
        elif segment["type"] == "at":
            message += MessageSegment.at(str(segment["data"].get("qq", "")))
        elif segment["type"] == "image":
            message += MessageSegment.image(str(segment["data"].get("file", "")))
    return message


def _command_name(event: MessageEvent) -> str:
    context = command_context_after_real_mention(event)
    return context.text.split(maxsplit=1)[0] if context and context.text else ""


async def daily_wife_command(event: MessageEvent) -> bool:
    command = _command_name(event)
    if command in DAILY_WIFE_COMMANDS:
        return True
    config = get_plugin_config(Config)
    return config.daily_wife_marriage_enabled and command in {
        "/结芬", *MARRIAGE_ACCEPT_COMMANDS, *MARRIAGE_REJECT_COMMANDS
    }


daily_wife = on_message(rule=Rule(daily_wife_command), priority=20, block=True)


@daily_wife.handle()
async def handle_daily_wife(bot: Bot, event: MessageEvent) -> None:
    command = _command_name(event)
    marriage_enabled = get_plugin_config(Config).daily_wife_marriage_enabled
    if command == "/结芬":
        await _handle_marriage_request(bot, event)
        return
    if command in MARRIAGE_ACCEPT_COMMANDS | MARRIAGE_REJECT_COMMANDS:
        await _handle_marriage_response(bot, event, command in MARRIAGE_ACCEPT_COMMANDS)
        return
    if not isinstance(event, GroupMessageEvent):
        await daily_wife.finish("今日群友只能在群聊中使用。")

    group_id, requester_id, date_key = str(event.group_id), str(event.user_id), today_date_key()
    cache_key = (group_id, requester_id)
    record = daily_wives.get(cache_key)
    if record and record.date_key == date_key:
        await daily_wife.finish(_native_message(format_daily_wife_existing(record, marriage_enabled)))
    _clear_expired_daily_wives(date_key)
    try:
        result = await bot.call_api("get_group_member_list", group_id=event.group_id)
        if not isinstance(result, list):
            raise RuntimeError("get_group_member_list returned invalid data")
        members = [member for member in result if isinstance(member, dict)]
    except Exception:
        LOGGER.exception("Failed to get group member list")
        await daily_wife.finish("获取群成员列表失败，请稍后再试。")
    candidates = wife_candidates(members, requester_id)
    if not candidates:
        await daily_wife.finish("群里暂时没有可以抽取的对象。")
    target = random.choice(candidates)
    record = DailyWifeRecord(date_key, str(target.get("user_id")), member_display_name(target))
    daily_wives[cache_key] = record
    await daily_wife.finish(_native_message(format_daily_wife_new(record, marriage_enabled)))


async def _handle_marriage_request(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await daily_wife.finish("结芬只能在群聊中使用。")
    group_id, requester_id, date_key = str(event.group_id), str(event.user_id), today_date_key()
    requester_key = (group_id, requester_id)
    _clear_expired_marriage_results(date_key)
    result = marriage_results.get(requester_key)
    if result and result.date_key == date_key:
        await daily_wife.finish(format_marriage_result(result))
    record = daily_wives.get(requester_key)
    if not record or record.date_key != date_key:
        await daily_wife.finish("请先输入：@bot /今日群友")
    current = marriage_proposals_by_requester.get(requester_key)
    if current and _proposal_is_active(current):
        await daily_wife.finish("已经发起结芬请求啦，请等待对方 2 分钟内回复。")
    if current:
        if current.date_key == date_key:
            _record_marriage_result(current, False)
            _remove_marriage_proposal(current)
            await daily_wife.finish(MARRIAGE_FAIL_MESSAGE)
        _remove_marriage_proposal(current)

    target_key = (group_id, record.target_id)
    target_current = marriage_proposals_by_target.get(target_key)
    if target_current and _proposal_is_active(target_current):
        await daily_wife.finish("对方已经有待回应的结芬请求啦，稍等一下再试。")
    if target_current:
        if target_current.date_key == date_key:
            _record_marriage_result(target_current, False)
        _remove_marriage_proposal(target_current)
    proposal = MarriageProposal(group_id, requester_id, record.target_id, record.target_name, date_key, time.time())
    marriage_proposals_by_requester[requester_key] = proposal
    marriage_proposals_by_target[target_key] = proposal
    proposal.timeout_task = asyncio.create_task(_marriage_timeout(bot, event, proposal))
    await daily_wife.finish(_native_message([
        at_segment(record.target_id), text_segment(" "), at_segment(requester_id),
        text_segment(" 想和你结芬~ 2分钟内回复：@bot /愿意 或 @bot /不愿意"),
    ]))


async def _handle_marriage_response(bot: Bot, event: MessageEvent, accepted: bool) -> None:
    if not isinstance(event, GroupMessageEvent):
        await daily_wife.finish("结芬回应只能在群聊中使用。")
    proposal = marriage_proposals_by_target.get((str(event.group_id), str(event.user_id)))
    if not proposal:
        await daily_wife.finish("当前没有待回应的结芬请求。")
    _record_marriage_result(proposal, accepted)
    _remove_marriage_proposal(proposal)
    await daily_wife.finish(MARRIAGE_SUCCESS_MESSAGE if accepted else MARRIAGE_FAIL_MESSAGE)


async def _marriage_timeout(bot: Bot, event: GroupMessageEvent, proposal: MarriageProposal) -> None:
    try:
        await asyncio.sleep(MARRIAGE_RESPONSE_SECONDS)
        if marriage_proposals_by_requester.get((proposal.group_id, proposal.requester_id)) is not proposal:
            return
        _record_marriage_result(proposal, False)
        _remove_marriage_proposal(proposal, cancel_timeout=False)
        await bot.send(event, MARRIAGE_FAIL_MESSAGE)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("Failed to send marriage timeout reply")


def _proposal_is_active(proposal: MarriageProposal) -> bool:
    return proposal.date_key == today_date_key() and time.time() - proposal.created_at < MARRIAGE_RESPONSE_SECONDS


def _remove_marriage_proposal(proposal: MarriageProposal, cancel_timeout: bool = True) -> None:
    requester_key = (proposal.group_id, proposal.requester_id)
    target_key = (proposal.group_id, proposal.target_id)
    if marriage_proposals_by_requester.get(requester_key) is proposal:
        marriage_proposals_by_requester.pop(requester_key, None)
    if marriage_proposals_by_target.get(target_key) is proposal:
        marriage_proposals_by_target.pop(target_key, None)
    if cancel_timeout and proposal.timeout_task and proposal.timeout_task is not asyncio.current_task():
        proposal.timeout_task.cancel()


def _record_marriage_result(proposal: MarriageProposal, accepted: bool) -> None:
    marriage_results[(proposal.group_id, proposal.requester_id)] = MarriageResultRecord(
        proposal.date_key, proposal.target_id, proposal.target_name,
        MARRIAGE_RESULT_ACCEPTED if accepted else MARRIAGE_RESULT_REJECTED,
    )


def _clear_expired_daily_wives(date_key: str) -> None:
    for key in [key for key, value in daily_wives.items() if value.date_key != date_key]:
        daily_wives.pop(key, None)


def _clear_expired_marriage_results(date_key: str) -> None:
    for key in [key for key, value in marriage_results.items() if value.date_key != date_key]:
        marriage_results.pop(key, None)


def reset_state() -> None:
    proposals = list({id(item): item for item in marriage_proposals_by_requester.values()}.values())
    for proposal in proposals:
        _remove_marriage_proposal(proposal)
    daily_wives.clear()
    marriage_results.clear()
    marriage_proposals_by_requester.clear()
    marriage_proposals_by_target.clear()


@get_driver().on_shutdown
async def shutdown() -> None:
    tasks = [proposal.timeout_task for proposal in marriage_proposals_by_requester.values()
             if proposal.timeout_task and not proposal.timeout_task.done()]
    reset_state()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
