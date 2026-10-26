"""Native group-scoped courtship request and response flow."""
from dataclasses import dataclass

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from infrastructure.onebot import at_segment, text_segment
from plugins.core import command_context_after_real_mention

COURTSHIP_ACCEPT_COMMANDS = {"/接受求偶", "/接受", "接受求偶", "接受"}
COURTSHIP_REJECT_COMMANDS = {
    "/拒绝求偶", "/不接受求偶", "/拒绝", "/不接受", "拒绝求偶", "不接受求偶", "拒绝", "不接受"
}
COURTSHIP_SUCCESS_MESSAGE = "接下来的内容需要付给观看"
COURTSHIP_REJECT_MESSAGE = "啊，哦，看来你只能自己解决了呢~"

__plugin_meta__ = PluginMetadata(
    name="求偶", description="向群成员发起求偶请求并等待对方回应",
    usage="@bot /求偶 @成员；@bot /接受求偶 或 /拒绝求偶",
)


@dataclass(frozen=True)
class CourtshipProposal:
    group_id: str
    requester_id: str
    target_id: str


proposals_by_requester: dict[tuple[str, str], CourtshipProposal] = {}
proposals_by_target: dict[tuple[str, str], CourtshipProposal] = {}


def _native_message(segments: list[dict]) -> Message:
    message = Message()
    for segment in segments:
        if segment["type"] == "text":
            message += MessageSegment.text(str(segment["data"].get("text", "")))
        elif segment["type"] == "at":
            message += MessageSegment.at(str(segment["data"].get("qq", "")))
    return message


def _command_name(event: MessageEvent) -> str:
    context = command_context_after_real_mention(event)
    return context.text.split(maxsplit=1)[0] if context and context.text else ""


async def courtship_command(event: MessageEvent) -> bool:
    command = _command_name(event)
    return command == "/求偶" or command in COURTSHIP_ACCEPT_COMMANDS | COURTSHIP_REJECT_COMMANDS


# Daily-wife uses priority 20 so its legacy generic /接受 and /拒绝 aliases win while marriage is enabled.
courtship = on_message(rule=Rule(courtship_command), priority=21, block=True)


@courtship.handle()
async def handle_courtship(bot: Bot, event: MessageEvent) -> None:
    command = _command_name(event)
    if command == "/求偶":
        await _handle_request(bot, event)
        return
    await _handle_response(bot, event, command in COURTSHIP_ACCEPT_COMMANDS)


async def _handle_request(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await courtship.finish("求偶只能在群聊中使用。")
    context = command_context_after_real_mention(event)
    if context is None or not context.mentions:
        await courtship.finish("用法：@bot /求偶 @求偶对象")
    group_id, requester_id, target_id = str(event.group_id), str(event.user_id), context.mentions[0]
    if target_id == requester_id:
        await courtship.finish("不能向自己求偶。")
    if target_id == str(event.self_id):
        await courtship.finish("不能向机器人求偶。")

    requester_key, target_key = (group_id, requester_id), (group_id, target_id)
    if requester_key in proposals_by_requester:
        await courtship.finish("你已经发起求偶啦，请等待对方回复。")
    if target_key in proposals_by_target:
        await courtship.finish("对方已经有待回应的求偶请求啦，稍等一下再试。")
    proposal = CourtshipProposal(group_id, requester_id, target_id)
    proposals_by_requester[requester_key] = proposal
    proposals_by_target[target_key] = proposal
    await courtship.finish(_native_message([
        at_segment(target_id), text_segment(" "), at_segment(requester_id),
        text_segment(" 向你求偶。你发情了，请问是否接受求偶？请回复：@bot /接受求偶 或 @bot /拒绝求偶"),
    ]))


async def _handle_response(bot: Bot, event: MessageEvent, accepted: bool) -> None:
    if not isinstance(event, GroupMessageEvent):
        await courtship.finish("求偶回应只能在群聊中使用。")
    proposal = proposals_by_target.get((str(event.group_id), str(event.user_id)))
    if not proposal:
        await courtship.finish("当前没有待回应的求偶请求。")
    _remove_proposal(proposal)
    await courtship.finish(_native_message([
        at_segment(proposal.requester_id),
        text_segment(f" {COURTSHIP_SUCCESS_MESSAGE if accepted else COURTSHIP_REJECT_MESSAGE}"),
    ]))


def _remove_proposal(proposal: CourtshipProposal) -> None:
    proposals_by_requester.pop((proposal.group_id, proposal.requester_id), None)
    proposals_by_target.pop((proposal.group_id, proposal.target_id), None)


def reset_state() -> None:
    proposals_by_requester.clear()
    proposals_by_target.clear()


@get_driver().on_shutdown
async def shutdown() -> None:
    reset_state()
