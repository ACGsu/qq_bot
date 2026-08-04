import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .common import CommandContext, at_segment, qq_avatar_segment, text_segment, today_date_key


MARRIAGE_RESPONSE_SECONDS = 2 * 60
MARRIAGE_ACCEPT_COMMANDS = {"/愿意", "愿意", "/同意", "同意", "/接受", "接受"}
MARRIAGE_REJECT_COMMANDS = {"/不愿意", "不愿意", "/拒绝", "拒绝", "/不要", "不要"}
MARRIAGE_RESULT_ACCEPTED = "accepted"
MARRIAGE_RESULT_REJECTED = "rejected"
MARRIAGE_FAIL_MESSAGE = "雑魚~雑魚~，被甩了捏~"
MARRIAGE_SUCCESS_MESSAGE = "正在制作结婚证（其实是生图ai没钱弄）"


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


def member_display_name(member: dict[str, Any]) -> str:
    for key in ("card", "nickname", "title"):
        value = str(member.get(key) or "").strip()
        if value:
            return value
    return "这位群友"


def wife_candidates(members: list[dict[str, Any]], requester_id: str) -> list[dict[str, Any]]:
    candidates = []
    for member in members:
        user_id = str(member.get("user_id") or "")
        if not user_id or user_id == requester_id:
            continue
        candidates.append(member)
    return candidates


def format_daily_wife_new(record: DailyWifeRecord) -> list[dict[str, Any]]:
    return [
        qq_avatar_segment(record.target_id),
        text_segment("\n"),
        at_segment(record.target_id),
        text_segment(f" {record.target_name}是你的今日群友老婆~\n结芬~：输入 @bot /结芬 发起结芬请求"),
    ]


def format_daily_wife_existing(record: DailyWifeRecord) -> list[dict[str, Any]]:
    return [
        text_segment("你已经抽取了今日的群友老婆："),
        at_segment(record.target_id),
        text_segment(f" {record.target_name}\n结芬~：输入 @bot /结芬 发起结芬请求"),
    ]


class DailyWifePlugin:
    def __init__(self) -> None:
        self.daily_wives: dict[tuple[str, str], DailyWifeRecord] = {}
        self.marriage_results: dict[tuple[str, str], MarriageResultRecord] = {}
        self.marriage_proposals_by_requester: dict[tuple[str, str], MarriageProposal] = {}
        self.marriage_proposals_by_target: dict[tuple[str, str], MarriageProposal] = {}

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name in {"/今日群友", "/结芬"} | MARRIAGE_ACCEPT_COMMANDS | MARRIAGE_REJECT_COMMANDS

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        command_name = context.text.split(maxsplit=1)[0] if context.text else ""
        if command_name == "/结芬":
            await self._handle_marriage_request(bot, websocket, event)
            return
        if command_name in MARRIAGE_ACCEPT_COMMANDS | MARRIAGE_REJECT_COMMANDS:
            await self._handle_marriage_response(bot, websocket, event, accepted=command_name in MARRIAGE_ACCEPT_COMMANDS)
            return

        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "今日群友只能在群聊中使用。")
            return

        group_id = str(event.get("group_id"))
        requester_id = str(event.get("user_id"))
        date_key = today_date_key()
        cache_key = (group_id, requester_id)
        record = self.daily_wives.get(cache_key)
        if record and record.date_key == date_key:
            await bot._send_reply(websocket, event, format_daily_wife_existing(record))
            return

        self._clear_expired_daily_wives(date_key)
        try:
            members = await bot._get_group_member_list(websocket, group_id)
        except Exception:
            bot.logger.exception("Failed to get group member list")
            await bot._send_reply(websocket, event, "获取群成员列表失败，请稍后再试。")
            return

        candidates = wife_candidates(members, requester_id)
        if not candidates:
            await bot._send_reply(websocket, event, "群里暂时没有可以抽取的对象。")
            return

        target = random.choice(candidates)
        record = DailyWifeRecord(
            date_key=date_key,
            target_id=str(target.get("user_id")),
            target_name=member_display_name(target),
        )
        self.daily_wives[cache_key] = record
        await bot._send_reply(websocket, event, format_daily_wife_new(record))

    def _clear_expired_daily_wives(self, date_key: str) -> None:
        expired_keys = [key for key, record in self.daily_wives.items() if record.date_key != date_key]
        for key in expired_keys:
            self.daily_wives.pop(key, None)

    async def _handle_marriage_request(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "结芬只能在群聊中使用。")
            return

        group_id = str(event.get("group_id"))
        requester_id = str(event.get("user_id"))
        date_key = today_date_key()
        requester_key = (group_id, requester_id)
        self._clear_expired_marriage_results(date_key)

        marriage_result = self.marriage_results.get(requester_key)
        if marriage_result and marriage_result.date_key == date_key:
            await bot._send_reply(websocket, event, format_marriage_result(marriage_result))
            return

        record = self.daily_wives.get((group_id, requester_id))
        if not record or record.date_key != date_key:
            await bot._send_reply(websocket, event, "请先输入：@bot /今日群友")
            return

        current = self.marriage_proposals_by_requester.get(requester_key)
        if current and self._proposal_is_active(current):
            await bot._send_reply(websocket, event, "已经发起结芬请求啦，请等待对方 2 分钟内回复。")
            return
        if current:
            if current.date_key == date_key:
                self._record_marriage_result(current, accepted=False)
                self._remove_marriage_proposal(current)
                await bot._send_reply(websocket, event, MARRIAGE_FAIL_MESSAGE)
                return
            self._remove_marriage_proposal(current)

        target_key = (group_id, record.target_id)
        target_current = self.marriage_proposals_by_target.get(target_key)
        if target_current and self._proposal_is_active(target_current):
            await bot._send_reply(websocket, event, "对方已经有待回应的结芬请求啦，稍等一下再试。")
            return
        if target_current:
            if target_current.date_key == date_key:
                self._record_marriage_result(target_current, accepted=False)
            self._remove_marriage_proposal(target_current)

        proposal = MarriageProposal(
            group_id=group_id,
            requester_id=requester_id,
            target_id=record.target_id,
            target_name=record.target_name,
            date_key=date_key,
            created_at=time.time(),
        )
        self.marriage_proposals_by_requester[requester_key] = proposal
        self.marriage_proposals_by_target[target_key] = proposal
        proposal.timeout_task = asyncio.create_task(self._marriage_timeout(bot, websocket, event, proposal))

        await bot._send_reply(
            websocket,
            event,
            [
                at_segment(record.target_id),
                text_segment(" "),
                at_segment(requester_id),
                text_segment(" 想和你结芬~ 2分钟内回复：@bot /愿意 或 @bot /不愿意"),
            ],
        )

    async def _handle_marriage_response(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        accepted: bool,
    ) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "结芬回应只能在群聊中使用。")
            return

        group_id = str(event.get("group_id"))
        target_id = str(event.get("user_id"))
        proposal = self.marriage_proposals_by_target.get((group_id, target_id))
        if not proposal:
            await bot._send_reply(websocket, event, "当前没有待回应的结芬请求。")
            return

        self._record_marriage_result(proposal, accepted=accepted)
        self._remove_marriage_proposal(proposal)
        await bot._send_reply(websocket, event, MARRIAGE_SUCCESS_MESSAGE if accepted else MARRIAGE_FAIL_MESSAGE)

    async def _marriage_timeout(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        proposal: MarriageProposal,
    ) -> None:
        try:
            await asyncio.sleep(MARRIAGE_RESPONSE_SECONDS)
            if self.marriage_proposals_by_requester.get((proposal.group_id, proposal.requester_id)) is not proposal:
                return
            self._record_marriage_result(proposal, accepted=False)
            self._remove_marriage_proposal(proposal, cancel_timeout=False)
            await bot._send_reply(websocket, event, MARRIAGE_FAIL_MESSAGE)
        except asyncio.CancelledError:
            raise
        except Exception:
            bot.logger.exception("Failed to send marriage timeout reply")

    def _proposal_is_active(self, proposal: MarriageProposal) -> bool:
        return proposal.date_key == today_date_key() and time.time() - proposal.created_at < MARRIAGE_RESPONSE_SECONDS

    def _remove_marriage_proposal(self, proposal: MarriageProposal, cancel_timeout: bool = True) -> None:
        requester_key = (proposal.group_id, proposal.requester_id)
        target_key = (proposal.group_id, proposal.target_id)
        if self.marriage_proposals_by_requester.get(requester_key) is proposal:
            self.marriage_proposals_by_requester.pop(requester_key, None)
        if self.marriage_proposals_by_target.get(target_key) is proposal:
            self.marriage_proposals_by_target.pop(target_key, None)
        if cancel_timeout and proposal.timeout_task and proposal.timeout_task is not asyncio.current_task():
            proposal.timeout_task.cancel()

    def _record_marriage_result(self, proposal: MarriageProposal, accepted: bool) -> None:
        self.marriage_results[(proposal.group_id, proposal.requester_id)] = MarriageResultRecord(
            date_key=proposal.date_key,
            target_id=proposal.target_id,
            target_name=proposal.target_name,
            result=MARRIAGE_RESULT_ACCEPTED if accepted else MARRIAGE_RESULT_REJECTED,
        )

    def _clear_expired_marriage_results(self, date_key: str) -> None:
        expired_keys = [key for key, record in self.marriage_results.items() if record.date_key != date_key]
        for key in expired_keys:
            self.marriage_results.pop(key, None)


def format_marriage_result(record: MarriageResultRecord) -> str:
    if record.result == MARRIAGE_RESULT_ACCEPTED:
        return MARRIAGE_SUCCESS_MESSAGE
    return MARRIAGE_FAIL_MESSAGE
