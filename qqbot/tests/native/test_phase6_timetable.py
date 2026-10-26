import io
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from PIL import Image

from services.timetable.models import TimetableCommand as CommandContext


CALENDAR = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:phase6-course\r\n"
    "SUMMARY:Course A\r\nDTSTART;TZID=Asia/Shanghai:20261024T095500\r\n"
    "DTEND;TZID=Asia/Shanghai:20261024T112000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
).encode()


def group_event(command: str, *, user_id=111, group_id=100, message_id=60):
    message = Message([MessageSegment.at("999"), MessageSegment.text(f" {command}")])
    return GroupMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "normal",
        "user_id": user_id, "message_type": "group", "message_id": message_id,
        "group_id": group_id, "message": message, "original_message": message.copy(),
        "raw_message": str(message), "font": 0,
        "sender": {"user_id": user_id, "nickname": "Tester", "card": "", "role": "member"},
    })


def private_event(command: str, *, user_id=111):
    message = Message([MessageSegment.at("999"), MessageSegment.text(f" {command}")])
    return PrivateMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "friend",
        "user_id": user_id, "message_type": "private", "message_id": 61,
        "message": message, "original_message": message.copy(), "raw_message": str(message),
        "font": 0, "sender": {"user_id": user_id, "nickname": "Tester"},
    })


def upload_notice(*, user_id=111, group_id=100, file_id="file-1", name="课表.ics"):
    return GroupUploadNoticeEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "notice", "notice_type": "group_upload",
        "user_id": user_id, "group_id": group_id,
        "file": {"id": file_id, "name": name, "size": len(CALENDAR), "busid": 102},
    })


def create_bot(context):
    return context.create_bot(
        base=Bot, adapter=context.create_adapter(base=Adapter), self_id="999"
    )


def addressed(module, user_id: str, text: str) -> Message:
    return module._native_message([
        {"type": "at", "data": {"qq": user_id}},
        {"type": "text", "data": {"text": " " + text}},
    ])


@pytest.fixture
def timetable_service(tmp_path, monkeypatch):
    from plugins import timetable as module

    service = module.TimetablePlugin(database_path=tmp_path / "timetable.sqlite3", images_enabled=False)
    monkeypatch.setattr(module, "timetable_service", service)
    yield service
    service.close()


@pytest.mark.asyncio
async def test_native_timetable_matcher_rejects_private_and_opens_group_session(
    app, timetable_service
):
    from plugins import timetable as module

    private = private_event("/导入课表")
    async with app.test_matcher(module.timetable) as context:
        bot = create_bot(context)
        context.receive_event(bot, private)
        context.should_pass_rule()
        context.should_call_api(
            "send_private_msg",
            {"user_id": 111, "message": Message("课表功能仅支持群聊，请在群内 @bot /导入课表。")},
            result={"message_id": 1},
        )

    event = group_event("/导入课表")
    prompt = (
        "已开启课表文件接收（10 分钟内有效）。\n"
        "请由你本人在本群上传一个 .ics 文件（最大 10 MiB），不支持 Excel。\n"
        "上传文件无需 @bot；收到文件后请 @bot /已导入 解析并正式入库。\n"
        "再次 @bot /导入课表 会清空本次暂存并重新开始；@bot /取消导入 可取消。"
    )
    async with app.test_matcher(module.timetable) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_api(
            "send_group_msg",
            {"group_id": 100, "message": addressed(module, "111", prompt)},
            result={"message_id": 2},
        )
    assert ("100", "111") in timetable_service.sessions


@pytest.mark.asyncio
async def test_native_upload_notice_receives_one_ics(app, timetable_service, monkeypatch):
    from plugins import timetable as module
    from infrastructure.timetable.files import inspect_calendar_payload

    event = group_event("/导入课表")
    await timetable_service.handle(
        module.NativeTimetableBot(AsyncMock()), None, module._event_payload(event),
        CommandContext("/导入课表", []),
    )
    monkeypatch.setattr(
        module, "download_calendar_file_http",
        AsyncMock(return_value=inspect_calendar_payload(CALENDAR)),
    )
    notice = upload_notice()
    received = (
        f"文件已接收：课表.ics（{len(CALENDAR)} 字节）。\n"
        "已通过基础格式检查，尚未解析或入库。\n"
        "@bot /已导入 可解析并正式入库；如需更换文件，请先 @bot /导入课表 重新开始。"
    )
    async with app.test_matcher(module.timetable_upload_notice) as context:
        bot = create_bot(context)
        context.receive_event(bot, notice)
        context.should_pass_rule()
        context.should_call_api(
            "get_group_file_url",
            {"group_id": "100", "file_id": "file-1"},
            result={"url": "https://download.example/lesson.ics"},
        )
        context.should_call_api(
            "send_group_msg",
            {"group_id": 100, "message": addressed(module, "111", received)},
            result={"message_id": 3},
        )
    assert timetable_service.sessions[("100", "111")].candidate is not None


@pytest.mark.asyncio
async def test_import_confirmation_persists_across_service_restart(
    tmp_path, monkeypatch
):
    from plugins import timetable as module
    from infrastructure.timetable.files import inspect_calendar_payload
    from services.timetable.ics import parse_calendar

    db_path = tmp_path / "persist.sqlite3"
    service = module.TimetablePlugin(database_path=db_path, images_enabled=False)
    raw_bot = AsyncMock()
    calls = []

    async def call_api(api, **params):
        calls.append((api, params))
        if api == "get_group_file_url":
            return {"url": "https://download.example/lesson.ics"}
        return {"message_id": len(calls)}

    raw_bot.call_api.side_effect = call_api
    bot = module.NativeTimetableBot(raw_bot)
    monkeypatch.setattr(
        module, "download_calendar_file_http",
        AsyncMock(return_value=inspect_calendar_payload(CALENDAR)),
    )
    parsed = parse_calendar(CALENDAR)
    monkeypatch.setattr(module, "parse_calendar_isolated", AsyncMock(return_value=parsed))
    start = group_event("/导入课表")
    await service.handle(bot, None, module._event_payload(start), CommandContext("/导入课表", []))
    await service.handle_notice(bot, None, module._event_payload(upload_notice()))
    confirm = group_event("/已导入", message_id=62)
    await service.handle(bot, None, module._event_payload(confirm), CommandContext("/已导入", []))
    assert ("100", "111") not in service.sessions
    assert any("导入成功，已写入数据库" in str(params["message"]) for api, params in calls if api == "send_group_msg")
    service.close()

    reopened = module.TimetablePlugin(database_path=db_path, images_enabled=False)
    stored = await reopened.store.get_import(("100", "111"))
    assert stored is not None
    assert stored.occurrence_count == 1
    reopened.close()


@pytest.mark.asyncio
async def test_cancel_clears_memory_without_deleting_saved_timetable(
    timetable_service
):
    from plugins import timetable as module
    from services.timetable.ics import parse_calendar

    key = ("100", "111")
    await timetable_service.store.save(key, "old.ics", "a" * 64, parse_calendar(CALENDAR))
    raw_bot = AsyncMock()
    raw_bot.call_api.return_value = {"message_id": 1}
    bot = module.NativeTimetableBot(raw_bot)
    await timetable_service._handle_command(bot, None, key, "/更新课表")
    assert key in timetable_service.sessions
    await timetable_service._handle_command(bot, None, key, "/取消导入")
    assert key not in timetable_service.sessions
    assert await timetable_service.store.get_import(key) is not None


@pytest.mark.asyncio
async def test_delete_command_removes_only_requesting_users_timetable(timetable_service):
    from plugins import timetable as module
    from services.timetable.ics import parse_calendar

    key = ("100", "111")
    other = ("100", "222")
    await timetable_service.store.save(key, "own.ics", "a" * 64, parse_calendar(CALENDAR))
    await timetable_service.store.save(other, "other.ics", "b" * 64, parse_calendar(CALENDAR))
    raw_bot = AsyncMock()
    raw_bot.call_api.return_value = {"message_id": 1}
    bot = module.NativeTimetableBot(raw_bot)

    await timetable_service._handle_command(bot, None, key, "/删除课表")

    assert await timetable_service.store.get_import(key) is None
    assert await timetable_service.store.get_import(other) is not None
    assert "已删除" in str(raw_bot.call_api.await_args.kwargs["message"])


@pytest.mark.asyncio
async def test_delete_command_without_saved_timetable_is_idempotent(timetable_service):
    from plugins import timetable as module

    raw_bot = AsyncMock()
    raw_bot.call_api.return_value = {"message_id": 1}
    bot = module.NativeTimetableBot(raw_bot)

    await timetable_service._handle_command(bot, None, ("100", "111"), "/删除课表")

    assert "尚未导入" in str(raw_bot.call_api.await_args.kwargs["message"])


@pytest.mark.asyncio
async def test_duplicate_upload_event_does_not_download_or_reply_twice(
    timetable_service, monkeypatch
):
    from plugins import timetable as module
    from infrastructure.timetable.files import inspect_calendar_payload

    raw_bot = AsyncMock()
    raw_bot.call_api.side_effect = lambda api, **params: (
        {"url": "https://download.example/lesson.ics"}
        if api == "get_group_file_url" else {"message_id": 1}
    )
    bot = module.NativeTimetableBot(raw_bot)
    await timetable_service._handle_command(bot, None, ("100", "111"), "/导入课表")
    download = AsyncMock(return_value=inspect_calendar_payload(CALENDAR))
    monkeypatch.setattr(module, "download_calendar_file_http", download)
    payload = module._event_payload(upload_notice())
    await timetable_service.handle_notice(bot, None, payload)
    await timetable_service.handle_notice(bot, None, payload)
    download.assert_awaited_once()
    file_calls = [
        call for call in raw_bot.call_api.await_args_list
        if call.args[0] == "get_group_file_url"
    ]
    assert len(file_calls) == 1


@pytest.mark.asyncio
async def test_current_course_query_uses_live_member_allowlist(
    timetable_service, monkeypatch
):
    from plugins import timetable as module
    from services.timetable.ics import SHANGHAI, parse_calendar

    await timetable_service.store.save(("100", "111"), "a.ics", "a" * 64, parse_calendar(CALENDAR))
    other = CALENDAR.replace(b"Course A", b"Course B")
    await timetable_service.store.save(("100", "222"), "b.ics", "b" * 64, parse_calendar(other))
    raw_bot = AsyncMock()

    async def call_api(api, **params):
        if api == "get_group_member_list":
            return [
                {"group_id": 100, "user_id": 111, "card": "Alice"},
                {"group_id": 100, "user_id": 333, "nickname": "Requester"},
            ]
        return {"message_id": 1}

    raw_bot.call_api.side_effect = call_api
    monkeypatch.setattr(module, "_now", lambda: datetime(2026, 10, 24, 10, 0, tzinfo=SHANGHAI))
    await timetable_service._query_courses(
        module.NativeTimetableBot(raw_bot), None, ("100", "333"), "/课ing"
    )
    sent = [call for call in raw_bot.call_api.await_args_list if call.args[0] == "send_group_msg"]
    assert sent
    text = str(sent[-1].kwargs["message"])
    assert "Alice" in text and "Course A" in text
    assert "Course B" not in text


@pytest.mark.asyncio
async def test_image_failure_falls_back_to_complete_text(timetable_service, monkeypatch):
    from plugins import timetable as module
    from services.timetable.ics import SHANGHAI, parse_calendar

    class BrokenImages:
        @asynccontextmanager
        async def request(self, key):
            raise RuntimeError("render unavailable")
            yield

        def close(self):
            pass

    await timetable_service.store.save(
        ("100", "111"), "a.ics", "a" * 64, parse_calendar(CALENDAR)
    )
    timetable_service.images = BrokenImages()
    raw_bot = AsyncMock()
    raw_bot.call_api.return_value = {"message_id": 1}
    monkeypatch.setattr(module, "_now", lambda: datetime(2026, 10, 24, 10, 0, tzinfo=SHANGHAI))
    await timetable_service._query_courses(
        module.NativeTimetableBot(raw_bot), None, ("100", "111"), "/今日课程",
        sender_name="Alice",
    )
    text = str(raw_bot.call_api.await_args.kwargs["message"])
    assert "图片暂不可用，以下为文字课表" in text
    assert "Course A" in text


class FakeResponse:
    def __init__(self, status, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self, chunk_size):
        for chunk in self.chunks:
            yield chunk


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def stream(self, method, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


class FakeSession:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeDriver:
    def __init__(self, responses):
        self.client = FakeClient(responses)

    def get_session(self, **kwargs):
        return FakeSession(self.client)


@pytest.mark.asyncio
async def test_calendar_http_driver_validates_each_redirect_and_limits():
    from infrastructure.timetable.files import TimetableFileError, download_calendar_file_http

    driver = FakeDriver([
        FakeResponse(302, {"Location": "https://cdn.example/final.ics"}),
        FakeResponse(200, {"Content-Length": str(len(CALENDAR))}, [CALENDAR]),
    ])
    result = await download_calendar_file_http(driver, "https://download.example/start")
    assert result.content == CALENDAR
    assert driver.client.urls == [
        "https://download.example/start", "https://cdn.example/final.ics"
    ]

    unsafe = FakeDriver([FakeResponse(302, {"Location": "file:///etc/passwd"})])
    with pytest.raises(TimetableFileError):
        await download_calendar_file_http(unsafe, "https://download.example/start")

    oversized = FakeDriver([
        FakeResponse(200, {"Content-Length": str(len(CALENDAR) + 1)}, [CALENDAR])
    ])
    with pytest.raises(TimetableFileError, match="大小"):
        await download_calendar_file_http(
            oversized, "https://download.example/start", len(CALENDAR)
        )


@pytest.mark.asyncio
async def test_avatar_http_driver_rejects_redirect_and_normalizes_png():
    from infrastructure.timetable.avatar import AVATAR_SIZE, fetch_avatar_http

    with Image.new("RGB", (80, 40), "teal") as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        png = output.getvalue()
    driver = FakeDriver([
        FakeResponse(200, {"Content-Type": "image/png", "Content-Length": str(len(png))}, [png])
    ])
    normalized = await fetch_avatar_http(driver, "00111")
    assert normalized is not None
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == (AVATAR_SIZE, AVATAR_SIZE)
    assert driver.client.urls == ["https://q1.qlogo.cn/g?b=qq&nk=111&s=100"]

    redirected = FakeDriver([
        FakeResponse(302, {"Content-Type": "image/png", "Location": "https://example.test"}, [png])
    ])
    assert await fetch_avatar_http(redirected, "111") is None


def test_timetable_config_mapping_and_native_module_status(tmp_path):
    from infrastructure.config import RuntimeConfig
    from services.plugin_catalog import NATIVE_MODULE_PLUGIN_IDS

    options = RuntimeConfig(timetable_db_path=(tmp_path / "db.sqlite3").resolve()).nonebot_options(
        {"plugin_settings": {"timetable": {"group_isolation_enabled": "false"}}}, {}
    )
    assert options["timetable_group_isolation_enabled"] is False
    assert options["timetable_db_path"] == (tmp_path / "db.sqlite3").resolve()
    assert "timetable" in NATIVE_MODULE_PLUGIN_IDS

