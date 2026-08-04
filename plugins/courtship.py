from dataclasses import dataclass
from typing import Any

from .common import CommandContext, at_segment, text_segment


COURTSHIP_ACCEPT_COMMANDS = {"/接受求偶", "/接受", "接受求偶", "接受"}
COURTSHIP_REJECT_COMMANDS = {"/拒绝求偶", "/不接受求偶", "/拒绝", "/不接受", "拒绝求偶", "不接受求偶", "拒绝", "不接受"}
COURTSHIP_SUCCESS_MESSAGE = "接下来的内容需要付给观看"
COURTSHIP_REJECT_MESSAGE = "啊，哦，看来你只能自己解决了呢~"


@dataclass(frozen=True)
class CourtshipProposal:
    group_id: str
    requester_id: str
    target_id: str


class CourtshipPlugin:
    def __init__(self) -> None:
        self.proposals_by_requester: dict[tuple[str, str], CourtshipProposal] = {}
        self.proposals_by_target: dict[tuple[str, str], CourtshipProposal] = {}

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == "/求偶" or command_name in COURTSHIP_ACCEPT_COMMANDS | COURTSHIP_REJECT_COMMANDS

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        command_name = context.text.split(maxsplit=1)[0] if context.text else ""
        if command_name == "/求偶":
            await self._handle_request(bot, websocket, event, context)
            return

        if command_name in COURTSHIP_ACCEPT_COMMANDS | COURTSHIP_REJECT_COMMANDS:
            await self._handle_response(
                bot,
                websocket,
                event,
                accepted=command_name in COURTSHIP_ACCEPT_COMMANDS,
            )

    async def _handle_request(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        context: CommandContext,
    ) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "求偶只能在群聊中使用。")
            return
        if not context.mentions:
            await bot._send_reply(websocket, event, "用法：@bot /求偶 @求偶对象")
            return

        group_id = str(event.get("group_id"))
        requester_id = str(event.get("user_id"))
        target_id = context.mentions[0]
        if target_id == requester_id:
            await bot._send_reply(websocket, event, "不能向自己求偶。")
            return
        bot_qq = str(bot.config.bot_qq or event.get("self_id") or "").strip()
        if bot_qq and target_id == bot_qq:
            await bot._send_reply(websocket, event, "不能向机器人求偶。")
            return

        requester_key = (group_id, requester_id)
        target_key = (group_id, target_id)
        if requester_key in self.proposals_by_requester:
            await bot._send_reply(websocket, event, "你已经发起求偶啦，请等待对方回复。")
            return
        if target_key in self.proposals_by_target:
            await bot._send_reply(websocket, event, "对方已经有待回应的求偶请求啦，稍等一下再试。")
            return

        proposal = CourtshipProposal(group_id=group_id, requester_id=requester_id, target_id=target_id)
        self.proposals_by_requester[requester_key] = proposal
        self.proposals_by_target[target_key] = proposal

        await bot._send_reply(
            websocket,
            event,
            [
                at_segment(target_id),
                text_segment(" "),
                at_segment(requester_id),
                text_segment(" 向你求偶。你发情了，请问是否接受求偶？请回复：@bot /接受求偶 或 @bot /拒绝求偶"),
            ],
        )

    async def _handle_response(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        accepted: bool,
    ) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "求偶回应只能在群聊中使用。")
            return

        group_id = str(event.get("group_id"))
        target_id = str(event.get("user_id"))
        proposal = self.proposals_by_target.get((group_id, target_id))
        if not proposal:
            await bot._send_reply(websocket, event, "当前没有待回应的求偶请求。")
            return

        self._remove_proposal(proposal)
        await bot._send_reply(
            websocket,
            event,
            [
                at_segment(proposal.requester_id),
                text_segment(f" {COURTSHIP_SUCCESS_MESSAGE if accepted else COURTSHIP_REJECT_MESSAGE}"),
            ],
        )

    def _remove_proposal(self, proposal: CourtshipProposal) -> None:
        self.proposals_by_requester.pop((proposal.group_id, proposal.requester_id), None)
        self.proposals_by_target.pop((proposal.group_id, proposal.target_id), None)
