"""Group-bound ICS sessions and configurable group-isolated/shared course queries."""

import asyncio
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from plugin_control import parse_bool_setting

from .common import (
    CommandContext, OneBotActionError, at_segment, image_segment_from_bytes, text_segment,
)
from .timetable_files import (
    MAX_ICS_BYTES,
    CalendarPayload,
    GroupFileUpload,
    TimetableFileError,
    download_calendar_file,
    extract_group_files,
    group_session_key,
    validate_download_url,
)
from .timetable_ics import CourseOccurrence, ParsedTimetable, SHANGHAI, TimetableParseError, parse_calendar_isolated
from .timetable_images import TimetableImageService
from .timetable_render import MemberView, TimetableView, render_page
from .timetable_store import TimetableConflictError, TimetableStore, TimetableStoreError


LOGGER = logging.getLogger("qq-bot")
SESSION_SECONDS = 10 * 60
MAX_BUFFERED_BYTES = 64 * 1024 * 1024
MAX_SESSIONS = 128
MAX_PENDING_FILES = 16
MAX_SESSION_PENDING_FILES = 4
MAX_SEEN_ENTRIES = 256
MAX_PENDING_PARSES = 8
MAX_BUFFERED_OCCURRENCES = 20_000
MAX_REPLY_CHARS = 2800
MAX_QUERY_MESSAGES = 8
IMAGE_NOTICE_SECONDS = 3
MAX_GROUP_MEMBERS = 10_000
COMMANDS = {"/课表", "/导入课表", "/取消导入", "/已导入", "/更新课表", "/课ing", "/今日课程"}
IMPORT_GUIDE = "请 @bot /导入课表 → 由本人在本群上传一个 .ics 文件 → @bot /已导入。"
SAVING_MESSAGE = "课表正在提交数据库，请稍后再操作；提交期间不重复导入，也不能取消或重开会话。"
SeenKey = TypeVar("SeenKey")


@dataclass(frozen=True)
class ReceivedTimetableFile:
    name: str
    payload: CalendarPayload


@dataclass
class TimetableImportSession:
    expires_at: float
    expected_revision: str | None = None
    candidate: ReceivedTimetableFile | None = None
    parsed: ParsedTimetable | None = None
    parse_error: str | None = None
    parse_task: asyncio.Task[ParsedTimetable] | None = None
    save_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_ids: set[str] = field(default_factory=set)
    seen_ids: dict[str, None] = field(default_factory=dict)
    seen_digests: dict[str, None] = field(default_factory=dict)
    rejected_metadata: dict[tuple[str, int | None], None] = field(default_factory=dict)
    expiry_handle: asyncio.TimerHandle | None = None

    @property
    def start_command(self) -> str:
        return "/更新课表" if self.expected_revision is not None else "/导入课表"


def _remember(entries: dict[SeenKey, None], value: SeenKey) -> None:
    entries[value] = None
    if len(entries) > MAX_SEEN_ENTRIES:
        entries.pop(next(iter(entries)))


def _display_name(name: str, limit: int = 120) -> str:
    return "".join(
        " " if unicodedata.category(char).startswith("C") or char in "\u2028\u2029" else char
        for char in name[:limit]
    ).strip()


def _group_member_names(group_id: str, members: object) -> dict[str, str]:
    # A fresh OneBot response is an allowlist, never a reason to expose departed users.
    if not isinstance(members, list) or not members or len(members) > MAX_GROUP_MEMBERS:
        raise ValueError("Invalid group member list")
    names: dict[str, str] = {}
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("Invalid group member entry")
        key = group_session_key({
            "group_id": member.get("group_id", group_id), "user_id": member.get("user_id"),
        })
        if key is None or key[0] != group_id:
            raise ValueError("Invalid group member identity")
        name = ""
        for field in ("card", "nickname"):
            value = member.get(field)
            if isinstance(value, str):
                name = _display_name(value, 60)
            if name:
                break
        names.setdefault(key[1], name or f"QQ {key[1]}")
    return names


def _sender_name(event: dict[str, Any], key: tuple[str, str]) -> str:
    sender = event.get("sender")
    if isinstance(sender, dict):
        identity = group_session_key({"group_id": key[0], "user_id": sender.get("user_id", key[1])})
        if identity == key:
            for field in ("card", "nickname"):
                value = sender.get(field)
                if isinstance(value, str) and (name := _display_name(value, 60)):
                    return name
    return f"QQ {key[1]}"


def _image_segment(plan, index, avatars):
    # Render and Base64 encode off the event loop; no filename or persistent PNG.
    return image_segment_from_bytes(render_page(plan, index, avatars))


def _image_reply_confirmed(response: object) -> bool:
    if (not isinstance(response, dict) or response.get("status") != "ok"
            or type(response.get("retcode")) is not int or response["retcode"] != 0):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    message_id = data.get("message_id")
    # NapCat message IDs may be signed integers.
    return type(message_id) is int or (
        isinstance(message_id, str) and re.fullmatch(r"-?[0-9]{1,20}", message_id) is not None
    )


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _clock(when: datetime) -> str:
    return when.strftime("%H:%M:%S" if when.second else "%H:%M")


def _course_line(item: CourseOccurrence) -> str:
    end = _clock(item.ends_at)
    if item.ends_at.date() != item.starts_at.date():
        end = f"{item.ends_at:%Y-%m-%d} {end}"
    location = f" · {_display_name(item.location, 200)}" if item.location else ""
    return (f"{item.starts_at:%Y-%m-%d} {_clock(item.starts_at)}–{end} "
            f"{_display_name(item.name, 200)}{location}")


def format_timetable_receipt(filename: str, result: ParsedTimetable, *, updated: bool = False) -> str:
    first = min(item.starts_at.date() for item in result.occurrences)
    last = max(item.ends_at.date() for item in result.occurrences)
    lines = [
        f"{'更新成功，已覆盖旧课表' if updated else '导入成功，已写入数据库'}：{_display_name(filename)}",
        f"识别 {result.source_event_count} 个课程事件、{result.series_count} 组课程计划，"
        f"展开 {len(result.occurrences)} 次课程。",
        f"日期范围：{first:%Y-%m-%d} ～ {last:%Y-%m-%d}（北京时间）",
        "前 5 次课程预览：" if len(result.occurrences) > 5 else "课程预览：",
    ]
    lines.extend(_course_line(item) for item in result.occurrences[:5])
    lines.extend(result.warnings)
    lines.append("可用 @bot /今日课程 查看本人今日课表，或 @bot /课ing 查看本群当前课程。")
    return "\n".join(lines)


class TimetablePlugin:
    plugin_id = "timetable"

    def __init__(
        self,
        settings: dict[str, str] | None = None,
        *,
        database_path: str | Path | None = None,
        session_seconds: float = SESSION_SECONDS,
        max_file_bytes: int = MAX_ICS_BYTES,
        max_buffered_bytes: int = MAX_BUFFERED_BYTES,
        max_sessions: int = MAX_SESSIONS,
        max_downloads: int = 3,
        max_parses: int = 2,
        max_buffered_occurrences: int = MAX_BUFFERED_OCCURRENCES,
        images_enabled: bool = True,
        image_service: TimetableImageService | None = None,
    ) -> None:
        if min(session_seconds, max_file_bytes, max_buffered_bytes, max_sessions,
               max_downloads, max_parses, max_buffered_occurrences) <= 0:
            raise ValueError("Timetable limits must be positive")
        self.group_isolation_enabled = parse_bool_setting(
            (settings or {}).get("group_isolation_enabled"), default=True,
        )
        self.store = TimetableStore(database_path, group_isolation_enabled=self.group_isolation_enabled)
        self.images = None
        if images_enabled:
            self.images = image_service if image_service is not None else TimetableImageService()
        self._closed = False
        self.session_seconds = session_seconds
        self.max_file_bytes = max_file_bytes
        self.max_buffered_bytes = max_buffered_bytes
        self.max_sessions = max_sessions
        self.sessions: dict[tuple[str, str], TimetableImportSession] = {}
        self._opening: dict[tuple[str, str], object] = {}
        self._buffered_bytes = 0
        self._pending_files = 0
        self._download_slots = asyncio.Semaphore(max_downloads)
        self._parse_slots = asyncio.Semaphore(max_parses)
        self._pending_parses = 0
        self.max_buffered_occurrences = max_buffered_occurrences
        self._buffered_occurrences = 0

    @property
    def scope_description(self) -> str:
        if self.group_isolation_enabled:
            return "群聊隔离：开启；各群分别导入、查询和更新。"
        return (
            "群聊隔离：关闭；同一 QQ 跨群共享最近更新的一份课表，其他共同群成员也可通过 /课ing 查阅。"
            "共享更新会替换该份课表，并保留其原所属群。"
        )

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name in COMMANDS

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        if self._closed:
            return
        key = group_session_key(event)
        if event.get("message_type") != "group" or key is None:
            await bot._send_reply(websocket, event, "课表功能仅支持群聊，请在群内 @bot /导入课表。")
            return
        command = context.text.split(maxsplit=1)[0] if context.text else ""
        try:
            await self._handle_command(bot, websocket, key, command, sender_name=_sender_name(event, key))
        except TimetableStoreError as exc:
            await self._reply(bot, websocket, key, str(exc))

    async def _handle_command(
        self, bot: Any, websocket: Any, key: tuple[str, str], command: str, *, sender_name: str | None = None,
    ) -> None:
        if command == "/课表":
            await self._reply(bot, websocket, key,
                "课表功能（仅支持 ICS）\n"
                f"{self.scope_description}\n"
                "@bot /课表：查看本说明\n"
                "@bot /导入课表 → 本人在本群上传一个 .ics 文件 → @bot /已导入\n"
                "@bot /已导入：解析课表并正式入库，成功后自动结束本次会话\n"
                "@bot /更新课表：上传新 ICS，再 /已导入，成功后覆盖本人的旧课表\n"
                "@bot /课ing：按群名片/昵称以图片显示仍在本群的已导入成员当前课程，无课显示无课程\n"
                "@bot /今日课程：以图片显示发送者自己的今日课表，并 @本人\n"
                "@bot /取消导入：清除本次暂存，不删除已入库课表\n"
                "查询时实时绘图，不保存生成图片；绘图失败时改用文字。\n"
                "不支持 Excel；查询按北京时间，结果在当前群内可见。\n"
                "上传会话到期或重启后失效；已入库课表会持久保存。")
            return
        if command in {"/课ing", "/今日课程"}:
            await self._query_courses(bot, websocket, key, command, sender_name=sender_name)
            return
        if command in {"/导入课表", "/更新课表"}:
            await self._begin_import(bot, websocket, key, command)
            return
        cancelled_open = command == "/取消导入" and self._opening.pop(key, None) is not None
        session = self._current_session(key)
        if session is None:
            if cancelled_open:
                await self._reply(bot, websocket, key, "已取消本次课表接收；已入库的课表不受影响。")
                return
            if command == "/已导入":
                saved = await self.store.get_import(key)
                if saved is not None:
                    await self._reply(bot, websocket, key,
                        f"你已导入课表（{saved.occurrence_count} 次课程），无需重复确认。\n"
                        "如需替换，请 @bot /更新课表；@bot /今日课程 可查看本人今日课表。")
                    return
            await self._reply(bot, websocket, key,
                "没有有效的课表接收会话（未开启、已取消、已过期或机器人已重启）。\n"
                "请先 @bot /导入课表；已有课表请 @bot /更新课表，再由本人在本群上传 .ics 文件。")
            return
        if command == "/取消导入":
            if self._discard_session(key, session):
                await self._reply(bot, websocket, key, "已取消本次课表接收并清除暂存文件；已入库的课表不受影响。")
            else:
                await self._reply(bot, websocket, key, SAVING_MESSAGE)
        elif command == "/已导入":
            await self._confirm_import(bot, websocket, key, session)

    async def _begin_import(self, bot: Any, websocket: Any, key: tuple[str, str], command: str) -> None:
        previous = self._current_session(key)
        if previous is not None and previous.save_task is not None:
            await self._reply(bot, websocket, key, SAVING_MESSAGE)
            return
        if previous is not None and previous.start_command == command:
            # Reopening cancels the old parser before waiting for a database lookup.
            self._discard_session(key, previous)
        token = object()
        self._opening[key] = token
        try:
            saved = await self.store.get_import(key)
            if self._opening.get(key) is not token:
                return
            if command == "/更新课表" and saved is None:
                await self._reply(bot, websocket, key, "你尚未导入课表，不能直接更新。\n" + IMPORT_GUIDE)
                return
            if command == "/导入课表" and saved is not None:
                await self._reply(bot, websocket, key, "你已导入课表，不能重复导入。请 @bot /更新课表 上传新文件覆盖。")
                return
            # Recheck after database I/O: another event may have started a commit.
            for existing_key in list(self.sessions):
                self._current_session(existing_key)
            if key not in self.sessions and len(self.sessions) >= self.max_sessions:
                await self._reply(bot, websocket, key, "当前课表接收会话已满，请稍后再试。")
                return
            previous = self.sessions.get(key)
            if previous is not None and not self._discard_session(key, previous):
                await self._reply(bot, websocket, key, SAVING_MESSAGE)
                return
            loop = asyncio.get_running_loop()
            session = TimetableImportSession(loop.time() + self.session_seconds,
                                             expected_revision=saved.revision if saved is not None else None)
            self.sessions[key] = session
            session.expiry_handle = loop.call_later(self.session_seconds, self._discard_session, key, session)
            update_note = "更新成功前，旧课表仍然有效。\n" if saved is not None else ""
            scope_note = "" if self.group_isolation_enabled else self.scope_description + "\n"
            await self._reply(bot, websocket, key,
                f"已开启课表{'更新' if saved is not None else '文件接收'}（{self.session_seconds / 60:g} 分钟内有效）。\n"
                f"请由你本人在本群上传一个 .ics 文件（最大 {self.max_file_bytes / (1024 * 1024):g} MiB），不支持 Excel。\n"
                "上传文件无需 @bot；收到文件后请 @bot /已导入 解析并正式入库。\n"
                f"{scope_note}{update_note}再次 @bot {session.start_command} 会清空本次暂存并重新开始；@bot /取消导入 可取消。")
        finally:
            if self._opening.get(key) is token:
                self._opening.pop(key)

    async def _confirm_import(
        self, bot: Any, websocket: Any, key: tuple[str, str], session: TimetableImportSession,
    ) -> None:
        if session.save_task is not None:
            await self._reply(bot, websocket, key, SAVING_MESSAGE)
            return
        candidate = session.candidate
        if candidate is None:
            message = (
                "文件正在接收，请稍后再 @bot /已导入。" if session.pending_ids else
                "尚未接收到有效文件，请由本人在本群上传 .ics 文件；不支持 Excel。"
            )
            await self._reply(bot, websocket, key, message)
            return
        if session.parsed is not None:
            await self._start_save(bot, websocket, key, session, candidate, session.parsed)
            return
        if session.parse_error is not None:
            await self._reply(bot, websocket, key, session.parse_error)
            return
        if session.parse_task is not None:
            await self._reply(bot, websocket, key, "课表正在解析或排队，请稍后再 @bot /已导入，无需重复上传。")
            return
        if self._pending_parses >= MAX_PENDING_PARSES:
            await self._reply(bot, websocket, key, "课表解析繁忙，请稍后再 @bot /已导入，无需重复上传。")
            return
        self._pending_parses += 1
        task = asyncio.create_task(self._parse_candidate(key, session, candidate))
        session.parse_task = task
        try:
            result = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return
        except Exception as exc:
            LOGGER.warning("Timetable parse failed (%s)", type(exc).__name__)
            if self._is_current(key, session):
                reason = str(exc) if isinstance(exc, TimetableParseError) else "解析未能完成，请稍后重试。"
                session.parse_error = (
                    f"课表解析失败：{reason}\n未入库，不会保留部分课程；旧课表不受影响。\n"
                    f"请 @bot {session.start_command} 重新开始，再上传修正后的 .ics 文件。"
                )
                await self._reply(bot, websocket, key, session.parse_error)
            return
        finally:
            self._pending_parses -= 1
            if session.parse_task is task:
                session.parse_task = None
        if not self._is_current(key, session) or session.candidate is not candidate:
            return
        if self._buffered_occurrences + len(result.occurrences) > self.max_buffered_occurrences:
            await self._reply(bot, websocket, key,
                "课程暂存空间已满，文件仍在。请取消不再使用的会话，稍后再 @bot /已导入。")
            return
        session.parsed = result
        self._buffered_occurrences += len(result.occurrences)
        await self._start_save(bot, websocket, key, session, candidate, result)

    async def _parse_candidate(
        self, key: tuple[str, str], session: TimetableImportSession, candidate: ReceivedTimetableFile,
    ) -> ParsedTimetable:
        async with self._parse_slots:
            if not self._is_current(key, session) or session.candidate is not candidate:
                raise asyncio.CancelledError
            return await parse_calendar_isolated(candidate.payload.content)

    async def _start_save(
        self, bot: Any, websocket: Any, key: tuple[str, str], session: TimetableImportSession,
        candidate: ReceivedTimetableFile, result: ParsedTimetable,
    ) -> None:
        if not self._is_current(key, session) or session.candidate is not candidate:
            return
        # This is the commit boundary. No await between the identity check and marking
        # the session as saving. Cancel/reopen/expiry cannot replace it during a write.
        task = asyncio.create_task(self._save_candidate(bot, websocket, key, session, candidate, result))
        session.save_task = task
        self._opening.pop(key, None)

        def finished(done: asyncio.Task) -> None:
            if not done.cancelled() and (error := done.exception()) is not None:
                LOGGER.warning("Timetable save completion failed (%s)", type(error).__name__)

        task.add_done_callback(finished)
        # A disconnected/cancelled WebSocket event must not release the session early.
        await asyncio.shield(task)

    async def _save_candidate(
        self, bot: Any, websocket: Any, key: tuple[str, str], session: TimetableImportSession,
        candidate: ReceivedTimetableFile, result: ParsedTimetable,
    ) -> None:
        try:
            await self.store.save(key, _display_name(candidate.name), candidate.payload.sha256, result,
                                  expected_revision=session.expected_revision)
        except Exception as exc:
            LOGGER.warning("Timetable save failed (%s)", type(exc).__name__)
            if self.sessions.get(key) is session:
                session.save_task = None
                if isinstance(exc, TimetableConflictError):
                    self._discard_session(key, session)
                    message = str(exc)
                else:
                    reason = str(exc) if isinstance(exc, TimetableStoreError) else "数据库写入未能完成，请稍后重试。"
                    retry = ("文件和解析结果仍在，请稍后 @bot /已导入 重试，无需重新上传。"
                             if self._is_current(key, session) else
                             f"会话已过期，请 @bot {session.start_command} 重新上传。")
                    message = f"课表保存失败：{reason}\n本次未入库，旧课表未改动。\n{retry}"
                await self._reply(bot, websocket, key, message)
        else:
            if self.sessions.get(key) is session:
                self._discard_session(key, session, force=True)
                await self._reply(bot, websocket, key,
                    format_timetable_receipt(candidate.name, result, updated=session.expected_revision is not None))
        finally:
            session.save_task = None

    async def _query_view(
        self, bot: Any, websocket: Any, key: tuple[str, str], command: str,
        names: dict[str, str], sender_name: str | None,
    ) -> TimetableView | None:
        if self._closed:
            return None
        # Always read live data and resolve the clock after network/queue waits.
        now = _now()
        if command == "/课ing":
            members = await self.store.current_courses(key[0], now, user_ids=tuple(names))
            if not members:
                await self._reply(bot, websocket, key, "本群当前成员尚未导入课表。\n" + IMPORT_GUIDE)
                return None
            duplicates = Counter(names[member.user_id] for member in members)
            views = tuple(MemberView(
                member.user_id,
                names[member.user_id] + (f"（QQ {member.user_id}）" if duplicates[names[member.user_id]] > 1 else ""),
                member.courses,
            ) for member in members)
            return TimetableView("current", now, views)
        courses = await self.store.today_courses(key, now)
        if courses is None:
            await self._reply(bot, websocket, key, "你尚未导入课表。\n" + IMPORT_GUIDE)
            return None
        return TimetableView("today", now, (MemberView(key[1], sender_name or f"QQ {key[1]}", courses),))

    @staticmethod
    def _query_lines(view: TimetableView) -> list[str]:
        if view.mode == "current":
            lines = [f"本群当前课程（{view.now:%Y-%m-%d} {_clock(view.now)}，北京时间）"]
            for member in view.members:
                if member.courses:
                    lines.extend(f"{member.name}：" + _course_line(course) for course in member.courses)
                else:
                    lines.append(f"{member.name}：无课程")
            return lines
        lines = [f"你的今日课程（{view.now:%Y-%m-%d}，北京时间）"]
        courses = view.members[0].courses
        lines.extend(_course_line(course) for course in courses)
        if not courses:
            lines.append("今日无课程。")
        return lines

    async def _query_courses(
        self, bot: Any, websocket: Any, key: tuple[str, str], command: str, *, sender_name: str | None = None,
    ) -> None:
        names = {}
        if command == "/课ing":
            try:
                names = _group_member_names(key[0], await bot._get_group_member_list(websocket, key[0]))
            except Exception as exc:
                LOGGER.warning("Timetable member lookup failed (%s)", type(exc).__name__)
                await self._reply(bot, websocket, key,
                    "获取群成员列表失败，暂不展示本群课表，以免显示已退群成员的数据。\n"
                    "请稍后重试；也可用 @bot /今日课程 查询本人的课表。")
                return
        view = await self._query_view(bot, websocket, key, command, names, sender_name)
        if view is None or self._closed:
            return
        if self.images is None:
            await self._reply_lines(bot, websocket, key, self._query_lines(view))
            return
        fallback = "图片暂不可用，以下为文字课表。"
        sent, total = 0, 0
        try:
            async with self.images.request((key, command)):
                avatars = await self.images.avatars.get_many(member.user_id for member in view.members)
                # Avatars may take a short wait. Requery at a fresh timestamp so
                # crossing 14:30, 15:55 or midnight cannot reuse the earlier state.
                view = await self._query_view(bot, websocket, key, command, names, sender_name)
                if view is None or self._closed:
                    return
                plan = await self.images.plan(view)
                total = len(plan.pages)
                for index in range(total):
                    if self._closed:
                        return
                    image = await self.images.work(_image_segment, plan, index, avatars)
                    try:
                        if self._closed:
                            return
                        event = {"message_type": "group", "group_id": int(key[0]), "user_id": int(key[1])}
                        label = f" {command}（{index + 1}/{total}）\n" if total > 1 else " "
                        try:
                            response = await bot._send_reply(websocket, event,
                                [at_segment(key[1]), text_segment(label), image], wait_for_response=True)
                        except OneBotActionError:
                            fallback = "图片发送被拒绝，以下为完整文字课表。"
                            break
                        except Exception as exc:
                            LOGGER.warning("Timetable image acknowledgement unavailable (%s)", type(exc).__name__)
                            await self._reply_unconfirmed(bot, websocket, key, index, total)
                            return
                        if not _image_reply_confirmed(response):
                            await self._reply_unconfirmed(bot, websocket, key, index, total)
                            return
                        sent += 1
                    finally:
                        # Neither the PNG nor its Base64 reply is retained after send.
                        del image
                else:
                    return
        except TimetableStoreError:
            raise
        except Exception as exc:
            # Includes missing Pillow/fonts, overflow, queue saturation and render
            # timeout. Cancellation deliberately propagates (BaseException).
            LOGGER.warning("Timetable image unavailable (%s)", type(exc).__name__)
        if self._closed:
            return
        if sent:
            fallback = f"图片仅确认发送 {sent}/{total} 页；以下为完整文字课表，以免遗漏。"
        await self._reply_lines(bot, websocket, key, [fallback, *self._query_lines(view)])

    async def _reply_unconfirmed(self, bot, websocket, key, index, total):
        # A timeout/disconnect/async result may already have delivered the image.
        # Never blindly send the same page or the complete schedule a second time.
        try:
            await asyncio.wait_for(self._reply(bot, websocket, key,
                f"第 {index + 1}/{total} 页图片发送结果未能确认，可能已发送；本次不自动重发，后续页已停止。"
                "请先查看群消息，若缺少课表再重新查询。"), timeout=IMAGE_NOTICE_SECONDS)
        except Exception as exc:
            # A disconnected socket may also reject the short warning. Do not
            # turn that into a duplicate full timetable via the rendering fallback.
            LOGGER.warning("Timetable image confirmation notice unavailable (%s)", type(exc).__name__)

    async def _reply_lines(self, bot: Any, websocket: Any, key: tuple[str, str], lines: list[str]) -> None:
        chunks: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > MAX_REPLY_CHARS and current:
                chunks.append(current)
                current = ""
            current += ("\n" if current else "") + line
        if current:
            chunks.append(current)
        if len(chunks) > MAX_QUERY_MESSAGES:
            await self._reply(bot, websocket, key,
                "课表查询结果过长，超过单次消息上限，本次未发送不完整课表；请使用 /今日课程 查询本人或精简课表内容。")
            return
        for index, chunk in enumerate(chunks, 1):
            prefix = f"课表查询（{index}/{len(chunks)}）\n" if len(chunks) > 1 else ""
            await self._reply(bot, websocket, key, prefix + chunk)

    async def handle_notice(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        if event.get("notice_type") == "group_upload":
            await self._receive_event(bot, websocket, event)

    async def handle_event(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        if event.get("message_type") == "group":
            await self._receive_event(bot, websocket, event)

    async def _receive_event(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        key = group_session_key(event)
        if key is None:
            return
        session = self._current_session(key)
        if session is None:
            return
        for upload in extract_group_files(event):
            # The snapshot binds this event to the original session even while awaiting I/O.
            if not self._is_current(key, session):
                return
            await self._receive_file(bot, websocket, key, session, upload)

    async def _receive_file(
        self, bot: Any, websocket: Any, key: tuple[str, str], session: TimetableImportSession,
        upload: GroupFileUpload,
    ) -> None:
        if upload.file_id in session.seen_ids or upload.file_id in session.pending_ids:
            return
        if len(session.pending_ids) >= MAX_SESSION_PENDING_FILES or self._pending_files >= MAX_PENDING_FILES:
            await self._reply(bot, websocket, key, "课表文件接收繁忙，请稍后重新上传。")
            return
        session.pending_ids.add(upload.file_id)
        self._pending_files += 1
        try:
            # Serialize one member's candidates; other members and WebSocket events continue.
            async with session.lock:
                if not self._is_current(key, session):
                    return
                error = None
                if not upload.name.lower().endswith(".ics"):
                    error = "仅支持 .ics 课表文件，不支持 Excel（.xls/.xlsx）或其他格式。"
                elif upload.size is not None and (upload.size < 0 or upload.size > self.max_file_bytes):
                    error = f"文件大小无效或超过 {self.max_file_bytes / (1024 * 1024):g} MiB 上限，请重新上传。"
                if error:
                    # Reject duplicate metadata once, without poisoning the content ID:
                    # NapCat may reuse an MD5 ID when a valid ICS is renamed and reuploaded.
                    rejection_key = (upload.name, upload.size)
                    if rejection_key not in session.rejected_metadata:
                        _remember(session.rejected_metadata, rejection_key)
                        await self._reply(bot, websocket, key, error)
                    return
                try:
                    async with self._download_slots:
                        if not self._is_current(key, session):
                            return
                        # NapCat 4.18.6 expects string IDs; busid and event-supplied URLs are unnecessary.
                        response = await bot._send_action_request(
                            websocket, "get_group_file_url",
                            {"group_id": upload.group_id, "file_id": upload.file_id}, timeout=15,
                        )
                        data = response.get("data") if isinstance(response, dict) else None
                        url = validate_download_url(data.get("url") if isinstance(data, dict) else None)
                        if not self._is_current(key, session):
                            return
                        payload = await asyncio.to_thread(download_calendar_file, url, self.max_file_bytes)
                except Exception as exc:
                    # Signed download URLs and calendar contents must never enter logs or replies.
                    LOGGER.warning("Timetable file receive failed (%s)", type(exc).__name__)
                    if self._is_current(key, session):
                        message = str(exc) if isinstance(exc, TimetableFileError) else "无法接收课表文件，请稍后重新上传 .ics 文件。"
                        await self._reply(bot, websocket, key, message)
                    return

                if not self._is_current(key, session):
                    return
                # Notice IDs and message-segment IDs can differ for the same upload.
                # Do not use (filename, size) as a duplicate key: distinct files can share both.
                if payload.sha256 in session.seen_digests or (
                    session.candidate is not None and payload.sha256 == session.candidate.payload.sha256
                ):
                    _remember(session.seen_ids, upload.file_id)
                    return
                if session.candidate is not None:
                    _remember(session.seen_ids, upload.file_id)
                    _remember(session.seen_digests, payload.sha256)
                    await self._reply(bot, websocket, key,
                        "本次会话已接收一个文件，新文件未替换暂存。\n"
                        f"如需更换，请先 @bot {session.start_command} 重新开始，再上传新的 .ics 文件。")
                    return
                if self._buffered_bytes + len(payload.content) > self.max_buffered_bytes:
                    await self._reply(bot, websocket, key, "课表暂存空间已满，请稍后重新上传或取消不再使用的接收会话。")
                    return
                session.candidate = ReceivedTimetableFile(upload.name, payload)
                self._buffered_bytes += len(payload.content)
                _remember(session.seen_ids, upload.file_id)
                _remember(session.seen_digests, payload.sha256)
                await self._reply(bot, websocket, key,
                    f"文件已接收：{_display_name(upload.name)}（{len(payload.content)} 字节）。\n"
                    "已通过基础格式检查，尚未解析或入库。\n"
                    f"@bot /已导入 可解析并正式入库；如需更换文件，请先 @bot {session.start_command} 重新开始。")
        finally:
            session.pending_ids.discard(upload.file_id)
            self._pending_files -= 1

    def _current_session(self, key: tuple[str, str]) -> TimetableImportSession | None:
        session = self.sessions.get(key)
        if session is not None and asyncio.get_running_loop().time() >= session.expires_at:
            if self._discard_session(key, session):
                return None
        return session

    def _is_current(self, key: tuple[str, str], session: TimetableImportSession) -> bool:
        return self._current_session(key) is session

    def _discard_session(
        self, key: tuple[str, str], session: TimetableImportSession, *, force: bool = False,
    ) -> bool:
        if self.sessions.get(key) is not session:
            return True
        if session.save_task is not None and not force:
            return False
        self.sessions.pop(key)
        if session.expiry_handle is not None:
            session.expiry_handle.cancel()
        if session.parse_task is not None:
            session.parse_task.cancel()
        if session.parsed is not None:
            self._buffered_occurrences -= len(session.parsed.occurrences)
            session.parsed = None
        session.parse_error = None
        if session.candidate is not None:
            self._buffered_bytes -= len(session.candidate.payload.content)
            session.candidate = None
        # An already running to_thread download cannot be stopped safely. Its result is
        # discarded by the identity check, without releasing its concurrency slot early.
        return True

    def close(self) -> None:
        self._closed = True
        self._opening.clear()
        for key, session in list(self.sessions.items()):
            # A write already submitted is allowed to finish, but no stale reply is sent.
            self._discard_session(key, session, force=True)
        if self.images is not None:
            self.images.close()
        self.store.close()

    async def _reply(self, bot: Any, websocket: Any, key: tuple[str, str], message: str) -> None:
        if self._closed:
            return
        # Upload notices have no message_type. Build a separate group reply envelope.
        event = {"message_type": "group", "group_id": int(key[0]), "user_id": int(key[1])}
        await bot._send_reply(websocket, event, [at_segment(key[1]), text_segment(" " + message)])
