"""Native NoneBot adapter for the timetable workflow."""

import asyncio
from pathlib import Path
from typing import Any

from nonebot import get_driver, get_plugin_config, on_message, on_type
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel

from infrastructure.onebot import OneBotActionError
from infrastructure.timetable.avatar import AvatarCache, fetch_avatar_http
from infrastructure.timetable.files import (
    download_calendar_file_http,
    extract_group_files,
    group_session_key,
)
from plugins.core import command_context_after_real_mention
from services.timetable.ics import parse_calendar_isolated
from services.timetable.images import TimetableImageService
from services.timetable.models import TimetableCommand
from services.timetable.workflow import (
    COMMANDS,
    IMAGE_NOTICE_SECONDS,
    LOGGER,
    TimetableWorkflow,
    _image_reply_confirmed,
    _image_segment,
    _now as _workflow_now,
)


__plugin_meta__ = PluginMetadata(
    name="课表",
    description="导入 ICS 课表并查询本群当前课程或本人今日、明日课程",
    usage="@bot /导入课表；@bot /已导入；@bot /删除课表；@bot /课ing；@bot /今日课程",
)


class Config(BaseModel):
    timetable_db_path: Path = Path("storage/db/timetable.sqlite3").resolve()
    timetable_group_isolation_enabled: bool = True


def _now():
    """Compatibility seam for deterministic adapter and workflow tests."""
    return _workflow_now()


async def _download_calendar(url: str, max_bytes: int):
    return await download_calendar_file_http(get_driver(), url, max_bytes)


class TimetablePlugin(TimetableWorkflow):
    """Compatibility name that injects NoneBot-owned network dependencies."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("download_calendar", _download_calendar)
        kwargs.setdefault("parse_calendar", lambda content: parse_calendar_isolated(content))
        kwargs.setdefault("now_factory", lambda: _now())
        kwargs.setdefault("image_notice_seconds", lambda: IMAGE_NOTICE_SECONDS)
        super().__init__(*args, **kwargs)


def _native_message(value: Any) -> Message:
    if isinstance(value, Message):
        return value
    if isinstance(value, str):
        return Message(value)
    message = Message()
    for segment in value if isinstance(value, list) else []:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment.get("type") == "text":
            message += MessageSegment.text(str(data.get("text") or ""))
        elif segment.get("type") == "at":
            message += MessageSegment.at(str(data.get("qq") or ""))
        elif segment.get("type") == "image":
            message += MessageSegment.image(str(data.get("file") or ""))
    return message


class NativeTimetableBot:
    """Translate the workflow's small OneBot port to the native Adapter API."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _call_api(self, action: str, **params):
        try:
            return await self.bot.call_api(action, **params)
        except ActionFailed as exc:
            raise OneBotActionError(str(exc)) from exc

    async def _send_reply(
        self, websocket: Any, event: dict[str, Any], message: Any, *, wait_for_response: bool = False,
    ) -> Any:
        native = _native_message(message)
        if event.get("message_type") == "group" or event.get("group_id") is not None:
            return await self._call_api(
                "send_group_msg", group_id=int(event["group_id"]), message=native
            )
        return await self._call_api(
            "send_private_msg", user_id=int(event["user_id"]), message=native
        )

    async def _get_group_member_list(self, websocket: Any, group_id: str) -> Any:
        return await self._call_api("get_group_member_list", group_id=int(group_id))

    async def _send_action_request(
        self, websocket: Any, action: str, params: dict[str, Any], timeout: float = 10,
    ) -> Any:
        return await asyncio.wait_for(self._call_api(action, **params), timeout=timeout)


timetable_service: TimetablePlugin | None = None


def _get_service() -> TimetablePlugin:
    global timetable_service
    if timetable_service is None:
        config = get_plugin_config(Config)
        avatars = AvatarCache(async_fetch=lambda qq: fetch_avatar_http(get_driver(), qq))
        images = TimetableImageService(avatars=avatars)
        timetable_service = TimetablePlugin(
            {"group_isolation_enabled": str(config.timetable_group_isolation_enabled)},
            database_path=config.timetable_db_path,
            image_service=images,
        )
    return timetable_service


def _event_payload(event: MessageEvent | GroupUploadNoticeEvent) -> dict[str, Any]:
    payload = event.model_dump()
    if isinstance(event, MessageEvent):
        payload["message"] = [
            {"type": segment.type, "data": dict(segment.data)}
            for segment in event.original_message
        ]
    return payload


async def timetable_command_rule(event: MessageEvent) -> bool:
    context = command_context_after_real_mention(event)
    command = context.text.split(maxsplit=1)[0] if context and context.text else ""
    return command in COMMANDS


timetable = on_message(rule=Rule(timetable_command_rule), priority=20, block=True)


@timetable.handle()
async def handle_timetable_command(bot: Bot, event: MessageEvent) -> None:
    context = command_context_after_real_mention(event)
    if context is None:
        return
    await _get_service().handle(
        NativeTimetableBot(bot),
        None,
        _event_payload(event),
        TimetableCommand(context.text, context.mentions),
    )


async def timetable_file_message_rule(event: GroupMessageEvent) -> bool:
    if timetable_service is None:
        return False
    payload = _event_payload(event)
    key = group_session_key(payload)
    return key in timetable_service.sessions and bool(extract_group_files(payload))


timetable_file_message = on_message(
    rule=Rule(timetable_file_message_rule), priority=19, block=False
)


@timetable_file_message.handle()
async def handle_timetable_file_message(bot: Bot, event: GroupMessageEvent) -> None:
    if timetable_service is not None:
        await timetable_service.handle_event(
            NativeTimetableBot(bot), None, _event_payload(event)
        )


async def timetable_upload_notice_rule(event: GroupUploadNoticeEvent) -> bool:
    if timetable_service is None:
        return False
    payload = _event_payload(event)
    key = group_session_key(payload)
    return key in timetable_service.sessions and bool(extract_group_files(payload))


timetable_upload_notice = on_type(
    GroupUploadNoticeEvent,
    rule=Rule(timetable_upload_notice_rule),
    priority=19,
    block=False,
)


@timetable_upload_notice.handle()
async def handle_timetable_upload_notice(bot: Bot, event: GroupUploadNoticeEvent) -> None:
    if timetable_service is not None:
        await timetable_service.handle_notice(
            NativeTimetableBot(bot), None, _event_payload(event)
        )


def reset_state() -> None:
    if timetable_service is None:
        return
    timetable_service._opening.clear()
    for key, session in list(timetable_service.sessions.items()):
        timetable_service._discard_session(key, session, force=True)


@get_driver().on_shutdown
async def shutdown() -> None:
    global timetable_service
    if timetable_service is not None:
        service, timetable_service = timetable_service, None
        await service.aclose()
