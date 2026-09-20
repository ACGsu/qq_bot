import asyncio
import io
import json
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import BotConfig, NapCatBot
from plugin_control import PLUGIN_DEFINITIONS, PluginConfig, load_plugin_config, save_plugin_config
from plugins.basic import dispatch_command
from plugins.common import CommandContext
from plugins.timetable import TimetablePlugin
from plugins.timetable_ics import SHANGHAI, TimetableParseError, parse_calendar
from plugins.timetable_store import TimetableStore, TimetableStoreError
from plugins.timetable_files import (
    MAX_ICS_BYTES,
    GroupFileUpload,
    TimetableFileError,
    _SafeRedirectHandler,
    download_calendar_file,
    extract_group_files,
    group_session_key,
    inspect_calendar_payload,
    validate_download_url,
)


# Synthetic fixture: no student identity or other metadata from the user's sample.
CALENDAR = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:test-course\r\n"
    "SUMMARY:Course A\r\nDTSTART;TZID=Asia/Shanghai:20260907T095500\r\n"
    "DTEND;TZID=Asia/Shanghai:20260907T112000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=8;INTERVAL=1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
).encode("utf-8")
OTHER_CALENDAR = CALENDAR.replace(b"Course A", b"Course B")
DOWNLOAD_URL = "https://download.example/lesson?token=do-not-log"


def upload_event(file_id="notice-id", name="课表.ics", size=None, group_id=100, user_id=111, message=False):
    common = {"group_id": group_id, "user_id": user_id, "self_id": 999}
    if message:
        return {
            **common, "post_type": "message", "message_type": "group",
            "message": [{"type": "file", "data": {
                "file_id": file_id, "file": name,
                "file_size": str(size if size is not None else len(CALENDAR)),
                "url": "file:///untrusted-event-url",
            }}],
        }
    return {
        **common, "post_type": "notice", "notice_type": "group_upload",
        "file": {"id": file_id, "name": name, "size": size if size is not None else len(CALENDAR), "busid": 102},
    }


def command_event(command, group_id=100, user_id=111):
    return {
        "post_type": "message", "message_type": "group", "group_id": group_id, "user_id": user_id,
        "self_id": 999, "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " " + command}},
        ],
    }


def reply_text(reply):
    message = reply[1]
    if isinstance(message, str):
        return message
    return "".join(segment["data"]["text"] for segment in message if segment["type"] == "text")


class FakeBot:
    def __init__(self):
        self.replies = []
        self.actions = []
        self.response = {"data": {"url": DOWNLOAD_URL}}
        self.action_error = None
        self._get_group_member_list = AsyncMock(return_value=[
            {"group_id": 100, "user_id": user} for user in (111, 112, 113, 114, 222, 333)
        ])

    async def _send_reply(self, websocket, event, message):
        self.replies.append((event, message))

    async def _send_action_request(self, websocket, action, params, timeout=10):
        self.actions.append((action, params, timeout))
        if self.action_error:
            raise self.action_error
        return self.response


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.file_action_sent = asyncio.Event()
        self.member_action_sent = asyncio.Event()

    async def send(self, raw):
        data = json.loads(raw)
        self.sent.append(data)
        if data.get("action") == "get_group_file_url":
            self.file_action_sent.set()
        if data.get("action") == "get_group_member_list":
            self.member_action_sent.set()


class FileEventNormalizationTest(unittest.TestCase):
    def test_napcat_group_upload_notice(self):
        result = extract_group_files(upload_event())
        self.assertEqual(result, [GroupFileUpload("100", "111", "notice-id", "课表.ics", len(CALENDAR))])

    def test_napcat_file_segment_uses_file_id_file_and_string_size(self):
        result = extract_group_files(upload_event(file_id="message-uuid", message=True))
        self.assertEqual(result, [GroupFileUpload("100", "111", "message-uuid", "课表.ics", len(CALENDAR))])

    def test_cq_file_segment_escapes_and_equals(self):
        event = upload_event(message=True)
        event["message"] = "[CQ:file,file=a&#44;b&#91;1&#93;&amp;.ics,file_id=opaque=123,file_size=42]"
        result = extract_group_files(event)
        self.assertEqual(result[0].name, "a,b[1]&.ics")
        self.assertEqual(result[0].file_id, "opaque=123")
        self.assertEqual(result[0].size, 42)
        event["message"] = "[CQ:file,file=a&amp;#44;.ics,file_id=x]"
        self.assertEqual(extract_group_files(event)[0].name, "a&#44;.ics")

    def test_ignores_private_messages_and_other_notices(self):
        event = upload_event(message=True)
        event["message_type"] = "private"
        self.assertEqual(extract_group_files(event), [])
        event = upload_event()
        event["notice_type"] = "group_increase"
        self.assertEqual(extract_group_files(event), [])

    def test_bad_metadata_does_not_crash(self):
        event = upload_event(message=True)
        event["message"] = [None, "file", {}, {"type": "file", "data": None},
                            {"type": "file", "data": {"file_id": 123}},
                            {"type": "file", "data": {"id": "ok", "name": "a.ics", "size": "unknown"}}]
        self.assertEqual(extract_group_files(event), [GroupFileUpload("100", "111", "ok", "a.ics", None)])

    def test_missing_file_id_is_ignored(self):
        event = upload_event()
        event["file"].pop("id")
        self.assertEqual(extract_group_files(event), [])

    def test_identity_normalization(self):
        self.assertEqual(group_session_key({"group_id": "00100", "user_id": 111}), ("100", "111"))
        for value in (None, True, -1, 0, "all", "１２３", "1" * 100):
            with self.subTest(value=value):
                self.assertIsNone(group_session_key({"group_id": 100, "user_id": value}))


class FakeHTTPResponse(io.BytesIO):
    def __init__(self, content, *, headers=None, url=DOWNLOAD_URL):
        super().__init__(content)
        self.headers = headers or {}
        self.url = url
        self.read_calls = 0

    def geturl(self):
        return self.url

    def read1(self, amount):
        self.read_calls += 1
        return super().read(amount)


class CalendarTransportTest(unittest.TestCase):
    def fetch(self, content=CALENDAR, *, headers=None, url=DOWNLOAD_URL, max_bytes=MAX_ICS_BYTES):
        response = FakeHTTPResponse(content, headers=headers, url=url)
        with patch("plugins.timetable_files.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            result = download_calendar_file(DOWNLOAD_URL, max_bytes)
        self.assertTrue(response.closed)
        return result

    def test_ics_wrapper_and_utf8_bom_are_accepted(self):
        self.assertEqual(inspect_calendar_payload(CALENDAR).content, CALENDAR)
        self.assertEqual(inspect_calendar_payload(b"\xef\xbb\xbf" + CALENDAR).content, b"\xef\xbb\xbf" + CALENDAR)
        self.assertEqual(len(inspect_calendar_payload(CALENDAR).sha256), 64)

    def test_lf_and_case_insensitive_envelope(self):
        self.assertTrue(inspect_calendar_payload(b"begin:vcalendar\nVERSION:2.0\nend:vcalendar").sha256)

    def test_phase_one_does_not_claim_event_validation(self):
        payload = inspect_calendar_payload(b"BEGIN:VCALENDAR\nnot-a-valid-course\nEND:VCALENDAR")
        self.assertTrue(payload.content)

    def test_empty_excel_fake_ics_and_invalid_utf8_are_rejected(self):
        for content in (b"", b"PK\x03\x04excel-file", b"not an ICS", b"\xff", b"BEGIN:VCALENDAR\n\x00\nEND:VCALENDAR",
                        b"BEGIN:VCALENDAR\nEND:VCALENDAR\nextra", b"BEGIN:VCALENDAREND:VCALENDAR"):
            with self.subTest(content=content), self.assertRaises(TimetableFileError):
                inspect_calendar_payload(content)

    def test_content_limit_is_enforced(self):
        with self.assertRaises(TimetableFileError):
            inspect_calendar_payload(CALENDAR, len(CALENDAR) - 1)

    def test_http_https_only_and_no_embedded_credentials(self):
        for url in (None, "", "file:///etc/passwd", "data:text/calendar,hello", "ftp://example/a",
                    "https://user:password@example/a", "https://example:bad/a", "https://example/\nsecret", "http:///no-host"):
            with self.subTest(url=url), self.assertRaises(TimetableFileError):
                validate_download_url(url)
        self.assertEqual(validate_download_url(DOWNLOAD_URL), DOWNLOAD_URL)
        self.assertEqual(validate_download_url("http://example/a.ics"), "http://example/a.ics")

    def test_download_with_content_length(self):
        self.assertEqual(self.fetch(headers={"Content-Length": str(len(CALENDAR))}).content, CALENDAR)

    def test_download_without_content_length(self):
        self.assertEqual(self.fetch().content, CALENDAR)

    def test_larger_declared_length_is_rejected_before_read(self):
        response = FakeHTTPResponse(CALENDAR, headers={"Content-Length": str(MAX_ICS_BYTES + 1)})
        with patch("plugins.timetable_files.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(TimetableFileError):
                download_calendar_file(DOWNLOAD_URL)
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(response.closed)

    def test_actual_stream_limit_is_enforced_when_size_is_missing_or_wrong(self):
        for headers in ({}, {"Content-Length": "1"}):
            with self.subTest(headers=headers), self.assertRaises(TimetableFileError):
                self.fetch(headers=headers, max_bytes=len(CALENDAR) - 1)

    def test_truncated_download_is_rejected(self):
        with self.assertRaises(TimetableFileError):
            self.fetch(headers={"Content-Length": str(len(CALENDAR) + 1)})

    def test_total_deadline_is_checked(self):
        with patch("plugins.timetable_files.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimetableFileError):
                self.fetch()

    def test_download_errors_do_not_expose_signed_url(self):
        with patch("plugins.timetable_files.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = OSError(DOWNLOAD_URL)
            with self.assertRaises(TimetableFileError) as caught:
                download_calendar_file(DOWNLOAD_URL)
        self.assertNotIn("do-not-log", str(caught.exception))

    def test_redirects_revalidate_scheme_and_credentials(self):
        handler = _SafeRedirectHandler()
        request = urllib.request.Request(DOWNLOAD_URL)
        for url in ("file:///tmp/a", "data:secret", "https://user:password@example/a"):
            with self.subTest(url=url), self.assertRaises(TimetableFileError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirect = handler.redirect_request(request, None, 302, "Found", {}, "https://example/next")
        self.assertEqual(redirect.full_url, "https://example/next")

    def test_final_response_url_is_validated(self):
        with self.assertRaises(TimetableFileError):
            self.fetch(url="file:///tmp/file.ics")

    def test_real_loopback_http_download_and_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/calendar.ics")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Length", str(len(CALENDAR)))
                self.end_headers()
                self.wfile.write(CALENDAR)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            result = download_calendar_file(f"http://127.0.0.1:{server.server_port}/redirect")
            self.assertEqual(result.content, CALENDAR)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class TimetablePluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "timetable.sqlite3"
        self.plugin = TimetablePlugin(images_enabled=False, database_path=self.db_path)
        self.bot = FakeBot()
        self.download = self.enterContext(patch(
            "plugins.timetable.download_calendar_file", return_value=inspect_calendar_payload(CALENDAR),
        ))

    async def asyncTearDown(self):
        self.plugin.close()
        self.assertEqual(self.plugin._buffered_bytes, 0)
        self.assertEqual(self.plugin._pending_files, 0)

    async def command(self, command="/导入课表", group_id=100, user_id=111):
        await self.plugin.handle(self.bot, None, command_event(command, group_id, user_id), CommandContext(command, []))

    async def receive(self, event=None):
        event = event or upload_event()
        if event["post_type"] == "notice":
            await self.plugin.handle_notice(self.bot, None, event)
        else:
            await self.plugin.handle_event(self.bot, None, event)

    def received_replies(self):
        return [reply for reply in self.bot.replies if "文件已接收：" in reply_text(reply)]

    async def test_no_session_does_not_download_or_reply(self):
        await self.receive()
        await self.receive(upload_event(message=True))
        self.assertEqual(self.bot.replies, [])
        self.assertEqual(self.bot.actions, [])
        self.download.assert_not_called()

    async def test_import_binds_sender_not_mentioned_member(self):
        event = command_event("/导入课表")
        await self.plugin.handle(self.bot, None, event, CommandContext("/导入课表", ["222"]))
        self.assertIn(("100", "111"), self.plugin.sessions)
        self.assertNotIn(("100", "222"), self.plugin.sessions)
        self.assertIn("不支持 Excel", reply_text(self.bot.replies[-1]))

    async def test_group_and_user_isolation(self):
        await self.command()
        await self.receive(upload_event(group_id=200))
        await self.receive(upload_event(user_id=222))
        self.download.assert_not_called()
        await self.receive()
        self.assertEqual(len(self.received_replies()), 1)

    async def test_private_command_and_upload_are_rejected(self):
        event = command_event("/导入课表")
        event["message_type"] = "private"
        await self.plugin.handle(self.bot, None, event, CommandContext("/导入课表", []))
        self.assertIn("仅支持群聊", reply_text(self.bot.replies[-1]))
        self.assertEqual(self.plugin.sessions, {})
        await self.command()
        event = upload_event(message=True)
        event["message_type"] = "private"
        await self.receive(event)
        self.download.assert_not_called()

    async def test_notice_reply_mentions_uploader_and_has_group_envelope(self):
        await self.command()
        event = upload_event()
        original = json.dumps(event)
        await self.receive(event)
        reply_event, segments = self.received_replies()[0]
        self.assertEqual(reply_event["message_type"], "group")
        self.assertEqual(reply_event["group_id"], 100)
        self.assertEqual(segments[0], {"type": "at", "data": {"qq": "111"}})
        self.assertEqual(json.dumps(event), original)
        self.assertEqual(self.bot.actions, [("get_group_file_url", {"group_id": "100", "file_id": "notice-id"}, 15)])

    async def test_file_message_ignores_untrusted_event_url(self):
        await self.command()
        await self.receive(upload_event(message=True))
        self.download.assert_called_once_with(DOWNLOAD_URL, MAX_ICS_BYTES)
        self.assertEqual(len(self.received_replies()), 1)

    async def test_cq_file_message_receives_without_mention(self):
        await self.command()
        event = upload_event(message=True)
        event["message"] = "[CQ:file,file=课表.ics,file_id=cq-id,file_size=100]"
        await self.receive(event)
        self.assertEqual(len(self.received_replies()), 1)

    async def test_same_id_duplicates_download_only_once(self):
        await self.command()
        await asyncio.gather(self.receive(), self.receive())
        await self.receive()
        self.download.assert_called_once()
        self.assertEqual(len(self.received_replies()), 1)

    async def test_notice_and_segment_with_distinct_ids_deduplicate_by_content(self):
        await self.command()
        await asyncio.gather(self.receive(), self.receive(upload_event(file_id="other-uuid", message=True)))
        self.assertEqual(self.download.call_count, 2)
        self.assertEqual(len(self.received_replies()), 1)
        self.assertEqual(self.plugin._buffered_bytes, len(CALENDAR))

    async def test_same_name_and_size_different_content_is_not_a_duplicate(self):
        self.assertEqual(len(CALENDAR), len(OTHER_CALENDAR))
        self.download.side_effect = [inspect_calendar_payload(CALENDAR), inspect_calendar_payload(OTHER_CALENDAR)]
        await self.command()
        await self.receive()
        await self.receive(upload_event(file_id="new-file"))
        self.assertEqual(self.download.call_count, 2)
        self.assertIn("新文件未替换", reply_text(self.bot.replies[-1]))
        self.assertEqual(self.plugin.sessions[("100", "111")].candidate.payload.content, CALENDAR)

    async def test_reopening_allows_a_new_candidate(self):
        await self.command()
        await self.receive()
        old = self.plugin.sessions[("100", "111")]
        await self.command()
        self.assertIsNone(old.candidate)
        self.assertEqual(self.plugin._buffered_bytes, 0)
        self.download.return_value = inspect_calendar_payload(OTHER_CALENDAR)
        await self.receive(upload_event(file_id="new-file"))
        self.assertEqual(self.plugin.sessions[("100", "111")].candidate.payload.content, OTHER_CALENDAR)

    async def test_extension_is_case_insensitive(self):
        await self.command()
        await self.receive(upload_event(name="课表.ICS"))
        self.assertEqual(len(self.received_replies()), 1)

    async def test_excel_and_other_extensions_are_rejected_before_api_call(self):
        await self.command()
        for index, name in enumerate(("课表.xlsx", "课表.xls", "课表.csv", "课表.ics.zip", "")):
            await self.receive(upload_event(file_id=str(index), name=name))
        self.assertEqual(self.bot.actions, [])
        self.download.assert_not_called()
        self.assertIn("仅支持 .ics", reply_text(self.bot.replies[-1]))

    async def test_metadata_rejection_does_not_poison_reuploaded_ics_content_id(self):
        await self.command()
        await self.receive(upload_event(name="课表.txt"))
        await self.receive(upload_event(name="课表.ics"))
        self.assertEqual(len(self.received_replies()), 1)
        self.download.assert_called_once()

    async def test_duplicate_unsupported_file_metadata_only_replies_once(self):
        await self.command()
        self.bot.replies.clear()
        await self.receive(upload_event(name="课表.xlsx"))
        await self.receive(upload_event(name="课表.xlsx", file_id="segment-id", message=True))
        self.assertEqual(len(self.bot.replies), 1)
        self.download.assert_not_called()

    async def test_oversized_announced_file_is_rejected_before_api_call(self):
        await self.command()
        await self.receive(upload_event(size=MAX_ICS_BYTES + 1))
        self.assertEqual(self.bot.actions, [])
        self.assertIn("超过", reply_text(self.bot.replies[-1]))

    async def test_api_failure_allows_retry_and_hides_url(self):
        await self.command()
        self.bot.action_error = RuntimeError(DOWNLOAD_URL)
        with self.assertLogs("qq-bot", level="WARNING") as logs:
            await self.receive()
        self.assertNotIn("do-not-log", "".join(logs.output) + reply_text(self.bot.replies[-1]))
        self.download.assert_not_called()
        self.bot.action_error = None
        await self.receive()
        self.assertEqual(len(self.received_replies()), 1)

    async def test_missing_or_unsafe_api_url_can_be_retried(self):
        await self.command()
        for response in ({}, {"data": []}, {"data": {"url": "file:///tmp/file.ics"}}):
            self.bot.response = response
            with self.assertLogs("qq-bot", level="WARNING"):
                await self.receive()
        self.download.assert_not_called()
        self.bot.response = {"data": {"url": DOWNLOAD_URL}}
        await self.receive()
        self.assertEqual(len(self.received_replies()), 1)

    async def test_failed_download_can_be_retried(self):
        await self.command()
        self.download.side_effect = [TimetableFileError("下载超时，请重试。"), inspect_calendar_payload(CALENDAR)]
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.receive()
        self.assertIsNone(self.plugin.sessions[("100", "111")].candidate)
        await self.receive()
        self.assertEqual(len(self.received_replies()), 1)

    async def test_cancel_clears_candidate_and_later_upload_is_ignored(self):
        await self.command()
        await self.receive()
        old = self.plugin.sessions[("100", "111")]
        await self.command("/取消导入")
        self.assertEqual(self.plugin.sessions, {})
        self.assertIsNone(old.candidate)
        self.assertTrue(old.expiry_handle.cancelled())
        await self.receive(upload_event(file_id="ignored"))
        self.assertEqual(self.download.call_count, 1)

    async def test_expiry_timer_releases_buffer_without_new_events(self):
        await self.command()
        await self.receive()
        session = self.plugin.sessions[("100", "111")]
        session.expiry_handle.cancel()
        loop = asyncio.get_running_loop()
        session.expires_at = loop.time() + 0.01
        session.expiry_handle = loop.call_later(0.01, self.plugin._discard_session, ("100", "111"), session)
        await asyncio.sleep(0.04)
        self.assertEqual(self.plugin.sessions, {})
        self.assertIsNone(session.candidate)
        self.assertEqual(self.plugin._buffered_bytes, 0)

    async def test_expired_session_is_checked_even_before_timer_fires(self):
        await self.command()
        self.plugin.sessions[("100", "111")].expires_at = asyncio.get_running_loop().time() - 1
        await self.receive()
        self.download.assert_not_called()
        self.assertEqual(self.plugin.sessions, {})
        await self.command("/已导入")
        self.assertIn("没有有效", reply_text(self.bot.replies[-1]))

    async def check_stale_download(self, action):
        await self.command()
        started = threading.Event()
        release = threading.Event()

        def blocked_download(*args):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test download was not released")
            return inspect_calendar_payload(CALENDAR)

        self.download.side_effect = blocked_download
        task = asyncio.create_task(self.receive())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            if action == "expire":
                self.plugin.sessions[("100", "111")].expires_at = asyncio.get_running_loop().time() - 1
            else:
                await self.command(action)
            replies_before_completion = len(self.bot.replies)
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
        self.assertEqual(len(self.bot.replies), replies_before_completion)
        self.assertEqual(self.plugin._buffered_bytes, 0)
        if action == "/导入课表":
            self.assertIsNone(self.plugin.sessions[("100", "111")].candidate)
        else:
            self.assertEqual(self.plugin.sessions, {})

    async def test_cancel_during_download_discards_late_result(self):
        await self.check_stale_download("/取消导入")

    async def test_reopen_during_download_discards_old_result(self):
        await self.check_stale_download("/导入课表")

    async def test_expire_during_download_discards_late_result(self):
        await self.check_stale_download("expire")

    async def test_storage_cap_and_recovery_after_cancel(self):
        self.plugin.max_buffered_bytes = len(CALENDAR)
        await self.command()
        await self.receive()
        await self.command(user_id=222)
        await self.receive(upload_event(user_id=222))
        self.assertIn("暂存空间已满", reply_text(self.bot.replies[-1]))
        self.assertIsNone(self.plugin.sessions[("100", "222")].candidate)
        await self.command("/取消导入")
        await self.receive(upload_event(user_id=222))
        self.assertIsNotNone(self.plugin.sessions[("100", "222")].candidate)
        self.assertEqual(self.plugin._buffered_bytes, len(CALENDAR))

    async def test_session_cap_and_expired_slot_reuse(self):
        self.plugin.max_sessions = 1
        await self.command()
        await self.command(user_id=222)
        self.assertEqual(len(self.plugin.sessions), 1)
        self.assertIn("会话已满", reply_text(self.bot.replies[-1]))
        self.plugin.sessions[("100", "111")].expires_at = asyncio.get_running_loop().time() - 1
        await self.command(user_id=222)
        self.assertIn(("100", "222"), self.plugin.sessions)

    async def test_concurrent_download_limit_does_not_block_other_commands(self):
        self.plugin.close()
        self.plugin = TimetablePlugin(images_enabled=False, database_path=self.db_path, max_downloads=2)
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        active = 0
        peak = 0

        def blocked_download(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
                if active == 2:
                    started.set()
            try:
                if not release.wait(5):
                    raise TimeoutError()
                return inspect_calendar_payload(CALENDAR)
            finally:
                with lock:
                    active -= 1

        self.download.side_effect = blocked_download
        for user in (111, 222, 333):
            await self.command(user_id=user)
        tasks = [asyncio.create_task(self.receive(upload_event(user_id=user))) for user in (111, 222, 333)]
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            self.assertEqual(len(self.bot.actions), 2)
            await asyncio.wait_for(self.command("/已导入"), 1)
            self.assertIn("正在接收", reply_text(self.bot.replies[-1]))
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(peak, 2)
        self.assertEqual(len(self.received_replies()), 3)

    async def test_pending_queue_limit_rejects_excess_without_an_api_call(self):
        await self.command()
        started = asyncio.Event()
        release = asyncio.Event()
        original_action = self.bot._send_action_request

        async def blocked_action(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_action(*args, **kwargs)

        self.bot._send_action_request = blocked_action
        task = asyncio.create_task(self.receive())
        try:
            await asyncio.wait_for(started.wait(), 2)
            with patch("plugins.timetable.MAX_PENDING_FILES", 1):
                await self.receive(upload_event(file_id="excess"))
            self.assertIn("接收繁忙", reply_text(self.bot.replies[-1]))
            self.assertEqual(self.plugin._pending_files, 1)
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
        self.download.assert_called_once()

    async def test_confirmation_imports_courses_and_releases_upload(self):
        await self.command("/已导入")
        self.assertIn("请先", reply_text(self.bot.replies[-1]))
        await self.command()
        await self.command("/已导入")
        self.assertIn("尚未接收到", reply_text(self.bot.replies[-1]))
        await self.receive()
        await self.command("/已导入")
        message = reply_text(self.bot.replies[-1])
        self.assertIn("导入成功，已写入数据库", message)
        self.assertIn("展开 8 次课程", message)
        self.assertEqual((await self.plugin.store.get_import(("100", "111"))).occurrence_count, 8)
        self.assertEqual(self.plugin.sessions, {})
        self.assertEqual(self.plugin._buffered_bytes, 0)

    async def test_filename_is_text_not_executable_cq_segments(self):
        await self.command()
        await self.receive(upload_event(name="课表[CQ:at,qq=all]\n.ics"))
        segments = self.received_replies()[0][1]
        self.assertEqual([part["type"] for part in segments], ["at", "text"])
        self.assertEqual(segments[0]["data"]["qq"], "111")
        self.assertIn("课表[CQ:at,qq=all] .ics", segments[1]["data"]["text"])

    async def test_help_states_persistence_queries_and_ics_only(self):
        await self.command("/课表")
        text = reply_text(self.bot.replies[-1])
        self.assertIn("持久保存", text)
        self.assertIn("解析课表", text)
        for command in ("/课表", "/导入课表", "/已导入", "/更新课表", "/课ing", "/今日课程", "/取消导入"):
            self.assertIn(command, text)
        self.assertIn("不支持 Excel", text)


class TimetableParseWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "timetable.sqlite3"
        self.plugin = TimetablePlugin(images_enabled=False, database_path=self.db_path)
        self.bot = FakeBot()
        self.expected = parse_calendar(CALENDAR)
        self.download = self.enterContext(patch(
            "plugins.timetable.download_calendar_file", return_value=inspect_calendar_payload(CALENDAR),
        ))
        self.parser = self.enterContext(patch(
            "plugins.timetable.parse_calendar_isolated", new_callable=AsyncMock, return_value=self.expected,
        ))

    async def asyncTearDown(self):
        self.plugin.close()
        self.assertEqual(self.plugin._buffered_bytes, 0)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        self.assertEqual(self.plugin._pending_parses, 0)

    async def command(self, command="/已导入", user_id=111, group_id=100):
        await self.plugin.handle(self.bot, None, command_event(command, group_id, user_id), CommandContext(command, []))

    async def prepare(self, user_id=111, group_id=100):
        await self.command("/导入课表", user_id, group_id)
        await self.plugin.handle_notice(self.bot, None, upload_event(user_id=user_id, group_id=group_id))

    async def test_upload_waits_for_explicit_confirmation(self):
        await self.prepare()
        self.parser.assert_not_awaited()
        self.assertIsNone(self.plugin.sessions[("100", "111")].parsed)
        await self.command()
        self.parser.assert_awaited_once_with(CALENDAR)
        self.assertNotIn(("100", "111"), self.plugin.sessions)
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))

    async def test_repeated_confirmation_is_idempotent(self):
        await self.prepare()
        await self.command()
        first = self.bot.replies[-1]
        saved = await self.plugin.store.get_import(("100", "111"))
        await self.command()
        self.parser.assert_awaited_once()
        self.assertIn("无需重复确认", reply_text(self.bot.replies[-1]))
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), saved)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        text = reply_text(first)
        self.assertIn("2026-09-07 ～ 2026-10-26", text)
        self.assertIn("09:55–11:20 Course A", text)
        self.assertIn("前 5 次", text)
        self.assertEqual(text.count("09:55–11:20"), 5)
        self.assertIn("导入成功", text)

    async def test_confirmation_uses_sender_not_mentioned_member(self):
        await self.prepare()
        await self.prepare(user_id=222)
        await self.plugin.handle(self.bot, None, command_event("/已导入"), CommandContext("/已导入", ["222"]))
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))
        self.assertIsNone(await self.plugin.store.get_import(("100", "222")))
        self.assertIsNone(self.plugin.sessions[("100", "222")].parsed)
        self.assertEqual(self.bot.replies[-1][1][0], {"type": "at", "data": {"qq": "111"}})

    async def test_other_group_cannot_confirm_current_groups_file(self):
        await self.prepare()
        await self.command(group_id=200)
        self.parser.assert_not_awaited()
        self.assertIn("没有有效", reply_text(self.bot.replies[-1]))

    async def test_failed_parse_is_cached_and_never_keeps_partial_courses(self):
        await self.prepare()
        self.parser.side_effect = TimetableParseError("重复规则无效。")
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command()
        message = reply_text(self.bot.replies[-1])
        self.assertIn("重复规则无效", message)
        self.assertIn("/导入课表", message)
        self.assertIn("未入库", message)
        self.assertIsNone(self.plugin.sessions[("100", "111")].parsed)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        await self.command()
        self.parser.assert_awaited_once()
        self.assertEqual(reply_text(self.bot.replies[-1]), message)
        self.parser.side_effect = None
        await self.prepare()
        await self.command()
        self.assertEqual(self.parser.await_count, 2)
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))

    async def test_unexpected_parse_error_is_redacted_in_reply_and_logs(self):
        await self.prepare()
        self.parser.side_effect = RuntimeError("private-student-token")
        with self.assertLogs("qq-bot", level="WARNING") as logs:
            await self.command()
        self.assertNotIn("private-student-token", reply_text(self.bot.replies[-1]) + "".join(logs.output))

    async def test_course_and_filename_are_text_not_cq_instructions(self):
        self.parser.return_value = parse_calendar(CALENDAR.replace(b"Course A", b"[CQ:at,qq=all]\\nCourse"))
        await self.prepare()
        await self.command()
        segments = self.bot.replies[-1][1]
        self.assertEqual([part["type"] for part in segments], ["at", "text"])
        self.assertIn("[CQ:at,qq=all] Course", segments[1]["data"]["text"])

    async def test_cancel_or_restart_releases_parsed_cache(self):
        for command in ("/取消导入", "/导入课表"):
            with self.subTest(command=command):
                await self.prepare()
                with patch.object(self.plugin.store, "save", side_effect=TimetableStoreError("繁忙")):
                    with self.assertLogs("qq-bot", level="WARNING"):
                        await self.command()
                old = self.plugin.sessions[("100", "111")]
                self.assertIsNotNone(old.parsed)
                await self.command(command)
                self.assertIsNone(old.parsed)
                self.assertIsNone(old.candidate)
                self.assertEqual(self.plugin._buffered_occurrences, 0)

    async def test_expiry_releases_parsed_cache(self):
        await self.prepare()
        with patch.object(self.plugin.store, "save", side_effect=TimetableStoreError("繁忙")):
            with self.assertLogs("qq-bot", level="WARNING"):
                await self.command()
        session = self.plugin.sessions[("100", "111")]
        session.expires_at = asyncio.get_running_loop().time() - 1
        await self.command()
        self.assertEqual(self.plugin.sessions, {})
        self.assertEqual(self.plugin._buffered_occurrences, 0)

    async def test_preview_memory_limit_allows_retry_without_reupload(self):
        self.plugin.max_buffered_occurrences = 7
        await self.prepare()
        await self.command()
        session = self.plugin.sessions[("100", "111")]
        self.assertIsNone(session.parsed)
        self.assertIsNotNone(session.candidate)
        self.assertIn("暂存空间已满", reply_text(self.bot.replies[-1]))
        self.plugin.max_buffered_occurrences = 8
        await self.command()
        self.assertIsNone(session.parsed)
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))
        self.download.assert_called_once()
        self.assertEqual(self.parser.await_count, 2)

    async def test_duplicate_confirmation_and_help_do_not_start_another_worker(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_parser(content):
            started.set()
            await release.wait()
            return self.expected

        self.parser.side_effect = slow_parser
        await self.prepare()
        task = asyncio.create_task(self.command())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(self.command(), 1)
            self.assertIn("正在解析", reply_text(self.bot.replies[-1]))
            self.parser.assert_awaited_once()
            await asyncio.wait_for(self.command("/课表"), 1)
            self.assertIn("课表功能", reply_text(self.bot.replies[-1]))
            await asyncio.wait_for(self.prepare(user_id=222), 1)
            self.assertIn(("100", "222"), self.plugin.sessions)
        finally:
            release.set()
            await task

    async def check_stale_parse(self, action, swallow_cancellation=False):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_parser(content):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                if not swallow_cancellation:
                    raise
                await release.wait()
            return self.expected

        self.parser.side_effect = slow_parser
        await self.prepare()
        old = self.plugin.sessions[("100", "111")]
        task = asyncio.create_task(self.command())
        try:
            await asyncio.wait_for(started.wait(), 1)
            if action == "expire":
                old.expires_at = asyncio.get_running_loop().time() - 1
                self.plugin._current_session(("100", "111"))
            elif action == "close":
                self.plugin.close()
            else:
                await self.command(action)
            before = len(self.bot.replies)
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.bot.replies), before)
        self.assertIsNone(old.parsed)
        self.assertIsNone(old.candidate)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        self.assertEqual(self.plugin._pending_parses, 0)
        if action == "/导入课表":
            self.assertIsNone(self.plugin.sessions[("100", "111")].parsed)

    async def test_cancel_expire_restart_and_close_stop_active_parser(self):
        for action in ("/取消导入", "/导入课表", "expire", "close"):
            with self.subTest(action=action):
                await self.check_stale_parse(action)

    async def test_late_result_never_repopulates_discarded_session(self):
        for action in ("/取消导入", "/导入课表", "expire", "close"):
            with self.subTest(action=action):
                await self.check_stale_parse(action, swallow_cancellation=True)

    async def test_outer_event_task_cancellation_is_propagated(self):
        started = asyncio.Event()

        async def slow_parser(content):
            started.set()
            await asyncio.Event().wait()

        self.parser.side_effect = slow_parser
        await self.prepare()
        task = asyncio.create_task(self.command())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.plugin._pending_parses, 0)
        self.assertIsNone(self.plugin.sessions[("100", "111")].parse_task)

    async def test_parser_concurrency_is_bounded(self):
        started, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        async def slow_parser(content):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                started.set()
            try:
                await release.wait()
                return self.expected
            finally:
                active -= 1

        self.parser.side_effect = slow_parser
        for user in range(111, 116):
            await self.prepare(user_id=user)
        tasks = [asyncio.create_task(self.command(user_id=user)) for user in range(111, 116)]
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(active, 2)
            self.assertEqual(self.plugin._pending_parses, 5)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        for user in range(111, 116):
            self.assertIsNotNone(await self.plugin.store.get_import(("100", str(user))))

    async def test_parser_queue_cap_preserves_file_for_retry(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_parser(content):
            started.set()
            await release.wait()
            return self.expected

        self.parser.side_effect = slow_parser
        await self.prepare()
        await self.prepare(user_id=222)
        with patch("plugins.timetable.MAX_PENDING_PARSES", 1):
            task = asyncio.create_task(self.command())
            try:
                await asyncio.wait_for(started.wait(), 1)
                await self.command(user_id=222)
                self.assertIn("解析繁忙", reply_text(self.bot.replies[-1]))
                self.assertIsNotNone(self.plugin.sessions[("100", "222")].candidate)
                self.parser.assert_awaited_once()
            finally:
                release.set()
                await task
        await self.command(user_id=222)
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "222")))

    async def test_cancelling_a_queued_parse_never_starts_its_worker(self):
        self.plugin._parse_slots = asyncio.Semaphore(1)
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_parser(content):
            started.set()
            await release.wait()
            return self.expected

        self.parser.side_effect = slow_parser
        await self.prepare()
        await self.prepare(user_id=222)
        first = asyncio.create_task(self.command())
        queued = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            queued = asyncio.create_task(self.command(user_id=222))
            await asyncio.sleep(0)
            await self.command("/取消导入", user_id=222)
            await asyncio.wait_for(queued, 1)
            self.parser.assert_awaited_once()
        finally:
            release.set()
            await first
            if queued is not None:
                await queued


# Keep the original text-mode regression suite; image workflows have their own tests.
class TimetablePersistenceWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db_path = Path(temp.name) / "timetable.sqlite3"
        self.plugin = TimetablePlugin(images_enabled=False, database_path=self.db_path)
        self.bot = FakeBot()
        self.download = self.enterContext(patch("plugins.timetable.download_calendar_file"))
        self.parser = self.enterContext(patch("plugins.timetable.parse_calendar_isolated", new_callable=AsyncMock))
        self.now = datetime(2026, 9, 7, 10, 0, tzinfo=SHANGHAI)
        self.enterContext(patch("plugins.timetable._now", return_value=self.now))

    async def asyncTearDown(self):
        self.plugin.close()
        self.assertEqual(self.plugin._buffered_bytes, 0)
        self.assertEqual(self.plugin._buffered_occurrences, 0)
        self.assertEqual(self.plugin._pending_parses, 0)
        self.assertEqual(self.plugin.store._pending, 0)
        self.assertFalse(self.plugin._opening)

    async def command(self, command, *, group_id=100, user_id=111, mentions=()):
        await self.plugin.handle(self.bot, None, command_event(command, group_id, user_id), CommandContext(command, list(mentions)))

    async def prepare(self, content=CALENDAR, *, update=False, group_id=100, user_id=111):
        await self.command("/更新课表" if update else "/导入课表", group_id=group_id, user_id=user_id)
        self.download.return_value = inspect_calendar_payload(content)
        self.parser.return_value = parse_calendar(content)
        await self.plugin.handle_notice(self.bot, None, upload_event(group_id=group_id, user_id=user_id))

    async def import_file(self, content=CALENDAR, *, group_id=100, user_id=111):
        await self.prepare(content, group_id=group_id, user_id=user_id)
        await self.command("/已导入", group_id=group_id, user_id=user_id)
        return await self.plugin.store.get_import((str(group_id), str(user_id)))

    def latest(self):
        return reply_text(self.bot.replies[-1])

    async def test_update_without_import_gives_complete_import_instructions(self):
        await self.command("/更新课表", mentions=["222"])
        for text in ("尚未导入", "/导入课表", ".ics", "/已导入"):
            self.assertIn(text, self.latest())
        self.assertEqual(self.plugin.sessions, {})
        self.assertEqual(self.bot.replies[-1][1][0], {"type": "at", "data": {"qq": "111"}})
        self.download.assert_not_called()

    async def test_import_is_not_an_implicit_overwrite(self):
        original = await self.import_file()
        await self.command("/导入课表")
        self.assertIn("不能重复导入", self.latest())
        self.assertIn("/更新课表", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), original)
        self.assertEqual(self.plugin.sessions, {})

    async def test_update_keeps_old_courses_until_confirmation_then_replaces(self):
        original = await self.import_file()
        await self.prepare(OTHER_CALENDAR, update=True)
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), original)
        await self.command("/课ing")
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Course B", self.latest())
        await self.command("/已导入")
        self.assertIn("更新成功，已覆盖旧课表", self.latest())
        saved = await self.plugin.store.get_import(("100", "111"))
        self.assertNotEqual(saved.revision, original.revision)
        self.assertEqual(saved.occurrence_count, 8)
        await self.command("/今日课程")
        self.assertIn("Course B", self.latest())
        self.assertNotIn("Course A", self.latest())
        self.assertEqual(self.plugin.sessions, {})

    async def test_invalid_update_preserves_old_import_and_uses_update_restart_hint(self):
        original = await self.import_file()
        await self.prepare(OTHER_CALENDAR, update=True)
        self.parser.side_effect = TimetableParseError("测试无效重复规则")
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command("/已导入")
        self.assertIn("旧课表不受影响", self.latest())
        self.assertIn("/更新课表", self.latest())
        self.assertNotIn("/导入课表", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), original)
        await self.command("/今日课程")
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Course B", self.latest())

    async def test_cancel_and_expire_update_never_delete_old_courses(self):
        original = await self.import_file()
        for action in ("cancel", "expire"):
            with self.subTest(action=action):
                await self.prepare(OTHER_CALENDAR, update=True)
                session = self.plugin.sessions[("100", "111")]
                if action == "cancel":
                    await self.command("/取消导入")
                    self.assertIn("不受影响", self.latest())
                else:
                    session.expires_at = asyncio.get_running_loop().time() - 1
                    self.plugin._current_session(("100", "111"))
                self.assertEqual(self.plugin.sessions, {})
                self.assertEqual(await self.plugin.store.get_import(("100", "111")), original)
        self.parser.assert_awaited_once()

    async def test_restarting_update_and_replacing_candidate_uses_correct_hint(self):
        await self.import_file()
        await self.prepare(OTHER_CALENDAR, update=True)
        old = self.plugin.sessions[("100", "111")]
        self.download.return_value = inspect_calendar_payload(CALENDAR)
        await self.plugin.handle_notice(self.bot, None, upload_event(file_id="another-file"))
        self.assertIn("先 @bot /更新课表", self.latest())
        await self.command("/更新课表")
        self.assertIsNone(old.candidate)
        new = self.plugin.sessions[("100", "111")]
        self.assertIsNot(new, old)
        self.assertIsNotNone(new.expected_revision)
        self.assertIsNone(new.candidate)

    async def test_update_only_modifies_senders_own_group_record(self):
        await self.import_file()
        other_user = await self.import_file(user_id=222)
        other_group = await self.import_file(group_id=200)
        await self.prepare(OTHER_CALENDAR, update=True)
        await self.command("/已导入", mentions=["222"])
        self.assertEqual(await self.plugin.store.get_import(("100", "222")), other_user)
        self.assertEqual(await self.plugin.store.get_import(("200", "111")), other_group)
        await self.command("/今日课程")
        self.assertIn("Course B", self.latest())

    async def test_database_failure_preserves_cached_parse_for_retry(self):
        await self.prepare()
        with patch.object(self.plugin.store, "save", side_effect=TimetableStoreError("测试数据库繁忙")):
            with self.assertLogs("qq-bot", level="WARNING"):
                await self.command("/已导入")
        self.assertIn("本次未入库", self.latest())
        self.assertIn("无需重新上传", self.latest())
        self.assertNotIn("导入成功", self.latest())
        self.assertIsNone(await self.plugin.store.get_import(("100", "111")))
        self.assertIsNotNone(self.plugin.sessions[("100", "111")].parsed)
        await self.command("/已导入")
        self.assertIn("导入成功", self.latest())
        self.parser.assert_awaited_once()
        self.download.assert_called_once()

    async def test_unexpected_save_failure_does_not_leak_exception_text(self):
        await self.prepare()
        with patch.object(self.plugin.store, "save", side_effect=RuntimeError("private-token")):
            with self.assertLogs("qq-bot", level="WARNING") as logs:
                await self.command("/已导入")
        self.assertNotIn("private-token", self.latest() + "".join(logs.output))
        self.assertIn("保存失败", self.latest())

    async def test_stale_update_revision_never_overwrites_a_newer_import(self):
        original = await self.import_file()
        await self.prepare(OTHER_CALENDAR, update=True)
        newer = parse_calendar(CALENDAR.replace(b"Course A", b"Newer version"))
        saved = await self.plugin.store.save(("100", "111"), "newer.ics", "c" * 64, newer,
                                             expected_revision=original.revision)
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command("/已导入")
        self.assertIn("本次未覆盖", self.latest())
        self.assertIn("/更新课表", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), saved)
        self.assertEqual(self.plugin.sessions, {})

    async def test_save_is_single_flight_and_cannot_be_replaced_mid_transaction(self):
        started, release = asyncio.Event(), asyncio.Event()
        original_save = self.plugin.store.save

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_save(*args, **kwargs)

        await self.prepare()
        with patch.object(self.plugin.store, "save", side_effect=delayed) as save:
            task = asyncio.create_task(self.command("/已导入"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                session = self.plugin.sessions[("100", "111")]
                for command in ("/已导入", "/取消导入", "/导入课表", "/更新课表"):
                    await self.command(command)
                    self.assertIn("正在提交数据库", self.latest())
                    self.assertIs(self.plugin.sessions[("100", "111")], session)
                session.expires_at = asyncio.get_running_loop().time() - 1
                self.assertIs(self.plugin._current_session(("100", "111")), session)
                await self.command("/课表")
                self.assertIn("课表功能", self.latest())
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
            save.assert_awaited_once()
        self.assertEqual(self.plugin.sessions, {})
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))

    async def test_cancelling_websocket_task_does_not_release_inflight_save(self):
        started, release = asyncio.Event(), asyncio.Event()
        original_save = self.plugin.store.save

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_save(*args, **kwargs)

        await self.prepare()
        with patch.object(self.plugin.store, "save", side_effect=delayed):
            task = asyncio.create_task(self.command("/已导入"))
            await asyncio.wait_for(started.wait(), 2)
            session = self.plugin.sessions[("100", "111")]
            save_task = session.save_task
            try:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.command("/取消导入")
                self.assertIn("正在提交数据库", self.latest())
                self.assertIs(session.save_task, save_task)
            finally:
                release.set()
                await asyncio.wait_for(save_task, 3)
        self.assertEqual(self.plugin.sessions, {})
        self.assertIn("导入成功", self.latest())
        self.assertIsNotNone(await self.plugin.store.get_import(("100", "111")))

    async def test_stop_finishes_submitted_transaction_without_late_reply(self):
        started, release = threading.Event(), threading.Event()
        original_save = self.plugin.store._save

        def delayed(*args):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return original_save(*args)

        await self.prepare()
        with patch.object(self.plugin.store, "_save", side_effect=delayed):
            task = asyncio.create_task(self.command("/已导入"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.plugin.close()
                before = len(self.bot.replies)
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        self.assertEqual(len(self.bot.replies), before)
        probe = TimetableStore(self.db_path)
        try:
            self.assertIsNotNone(await probe.get_import(("100", "111")))
        finally:
            probe.close()

    async def test_cancel_or_close_while_session_lookup_pending_cannot_open_late_session(self):
        for action in ("cancel", "close"):
            with self.subTest(action=action):
                started, release = asyncio.Event(), asyncio.Event()

                async def delayed(key):
                    started.set()
                    await release.wait()
                    return None

                with patch.object(self.plugin.store, "get_import", side_effect=delayed):
                    task = asyncio.create_task(self.command("/导入课表"))
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        if action == "cancel":
                            await self.command("/取消导入")
                        else:
                            self.plugin.close()
                        before = len(self.bot.replies)
                    finally:
                        release.set()
                        await task
                self.assertEqual(len(self.bot.replies), before)
                self.assertEqual(self.plugin.sessions, {})

    async def test_query_current_shows_all_imported_members_but_not_other_groups(self):
        await self.import_file()
        later = CALENDAR.replace(b"095500", b"145500").replace(b"112000", b"162000")
        await self.import_file(later, user_id=222)
        await self.import_file(CALENDAR.replace(b"Course A", b"Other group secret"), group_id=200)
        await self.command("/课ing", user_id=333)
        self.assertIn("QQ 111", self.latest())
        self.assertIn("09:55–11:20 Course A", self.latest())
        self.assertIn("QQ 222：无课程", self.latest())
        self.assertNotIn("QQ 333", self.latest())
        self.assertNotIn("Other group secret", self.latest())
        self.assertEqual([s["data"]["qq"] for s in self.bot.replies[-1][1] if s["type"] == "at"], ["333"])

    async def test_current_prefers_group_card_then_nickname_then_qq(self):
        for user in (111, 222, 333):
            await self.import_file(user_id=user)
        self.bot._get_group_member_list.return_value = [
            {"user_id": 111, "card": " 群名片 ", "nickname": "不应显示的昵称"},
            {"user_id": 222, "card": "\t\n", "nickname": "QQ昵称"},
            {"user_id": 333, "card": None, "nickname": False},
        ]
        await self.command("/课ing")
        self.assertIn("群名片：", self.latest())
        self.assertIn("QQ昵称：", self.latest())
        self.assertIn("QQ 333：", self.latest())
        self.assertNotIn("不应显示的昵称", self.latest())
        self.bot._get_group_member_list.assert_awaited_once_with(None, "100")
        self.assertEqual([s["data"]["qq"] for s in self.bot.replies[-1][1] if s["type"] == "at"], ["111"])

    async def test_current_refreshes_names_and_membership_without_deleting_departed_imports(self):
        await self.import_file()
        original = await self.import_file(OTHER_CALENDAR, user_id=222)
        self.bot._get_group_member_list.return_value = [
            {"user_id": 111, "card": "原名"}, {"user_id": 222, "card": "将退群成员"},
        ]
        await self.command("/课ing")
        self.assertIn("将退群成员：", self.latest())
        self.bot._get_group_member_list.return_value = [{"user_id": 111, "card": "新名"}]
        await self.command("/课ing")
        self.assertIn("新名：", self.latest())
        self.assertNotIn("原名", self.latest())
        self.assertNotIn("将退群成员", self.latest())
        self.assertNotIn("Course B", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "222")), original)
        self.bot._get_group_member_list.return_value.append({"user_id": 222, "nickname": "重新入群"})
        await self.command("/课ing")
        self.assertIn("重新入群：", self.latest())
        self.assertIn("Course B", self.latest())
        self.assertEqual(self.bot._get_group_member_list.await_count, 3)

    async def test_only_departed_imports_are_not_reported_as_current_members(self):
        original = await self.import_file(OTHER_CALENDAR, user_id=222)
        self.bot._get_group_member_list.return_value = [{"user_id": 111}]
        await self.command("/课ing")
        self.assertIn("本群当前成员尚未导入课表", self.latest())
        self.assertIn("/导入课表", self.latest())
        self.assertNotIn("Course B", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "222")), original)

    async def test_duplicate_display_names_are_disambiguated_even_after_truncation(self):
        await self.import_file()
        await self.import_file(OTHER_CALENDAR, user_id=222)
        self.bot._get_group_member_list.return_value = [
            {"user_id": 111, "card": "同" * 60 + "一"},
            {"user_id": 222, "card": "同" * 60 + "二"},
        ]
        await self.command("/课ing")
        self.assertIn("同" * 60 + "（QQ 111）：", self.latest())
        self.assertIn("同" * 60 + "（QQ 222）：", self.latest())
        self.assertEqual(self.latest().count("Course A"), 1)
        self.assertEqual(self.latest().count("Course B"), 1)

    async def test_member_ids_are_normalized_and_duplicate_entries_do_not_duplicate_courses(self):
        await self.import_file()
        self.bot._get_group_member_list.return_value = [
            {"group_id": "00100", "user_id": "00111", "card": "只一次"},
            {"group_id": 100, "user_id": 111, "card": "重复记录"},
        ]
        await self.command("/课ing")
        self.assertEqual(self.latest().count("Course A"), 1)
        self.assertIn("只一次：", self.latest())
        self.assertNotIn("重复记录", self.latest())

    async def test_member_name_cq_and_line_separators_remain_plain_text(self):
        await self.import_file()
        self.bot._get_group_member_list.return_value = [
            {"user_id": 111, "card": "[CQ:at,qq=all]\u2028张三\u2029\n\x00"},
        ]
        await self.command("/课ing")
        segments = self.bot.replies[-1][1]
        self.assertEqual([part["type"] for part in segments], ["at", "text"])
        self.assertEqual(segments[0]["data"]["qq"], "111")
        self.assertIn("[CQ:at,qq=all] 张三：", self.latest())
        self.assertEqual(len(self.latest().splitlines()), 2)

    async def test_member_lookup_failure_never_falls_back_to_stored_users_or_logs_private_detail(self):
        await self.import_file()
        self.bot._get_group_member_list.side_effect = TimeoutError("private-token-and-names")
        with patch.object(self.plugin.store, "current_courses", new_callable=AsyncMock) as query:
            with self.assertLogs("qq-bot", level="WARNING") as logs:
                await self.command("/课ing")
            query.assert_not_awaited()
        self.assertIn("获取群成员列表失败", self.latest())
        self.assertNotIn("Course A", self.latest())
        self.assertNotIn("private-token-and-names", self.latest() + "".join(logs.output))
        await self.command("/今日课程")
        self.assertIn("Course A", self.latest())
        self.bot._get_group_member_list.assert_awaited_once()

    async def test_invalid_or_wrong_group_member_response_is_rejected_without_partial_query(self):
        await self.import_file()
        invalid = (None, {}, [], [None], [{}], [{"user_id": True}], [{"user_id": "１"}],
                   [{"user_id": -111}], [{"user_id": 111, "group_id": 200}],
                   [{"user_id": 111}, {"user_id": "invalid"}])
        with patch.object(self.plugin.store, "current_courses", new_callable=AsyncMock) as query:
            for response in invalid:
                with self.subTest(response=response), self.assertLogs("qq-bot", level="WARNING"):
                    self.bot._get_group_member_list.return_value = response
                    await self.command("/课ing")
                    self.assertIn("获取群成员列表失败", self.latest())
                    self.assertNotIn("Course A", self.latest())
            query.assert_not_awaited()

    async def test_member_response_size_limit_does_not_return_partial_results(self):
        await self.import_file()
        with patch("plugins.timetable.MAX_GROUP_MEMBERS", 1), self.assertLogs("qq-bot", level="WARNING"):
            await self.command("/课ing")
        self.assertIn("获取群成员列表失败", self.latest())
        self.assertNotIn("Course A", self.latest())

    async def test_current_time_is_taken_after_member_lookup_finishes(self):
        await self.import_file()
        clock = [datetime(2026, 9, 7, 11, 19, tzinfo=SHANGHAI)]

        async def crosses_lesson_end(*args):
            clock[0] = datetime(2026, 9, 7, 11, 20, tzinfo=SHANGHAI)
            return [{"user_id": 111, "card": "下课成员"}]

        self.bot._get_group_member_list.side_effect = crosses_lesson_end
        with patch("plugins.timetable._now", side_effect=lambda: clock[0]):
            await self.command("/课ing")
        self.assertIn("11:20", self.latest())
        self.assertIn("下课成员：无课程", self.latest())
        self.assertNotIn("Course A", self.latest())

    async def test_stop_while_member_lookup_pending_does_not_send_a_late_reply(self):
        await self.import_file()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            started.set()
            await release.wait()
            return [{"user_id": 111}]

        self.bot._get_group_member_list.side_effect = blocked
        task = asyncio.create_task(self.command("/课ing"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            before = len(self.bot.replies)
            self.plugin.close()
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.bot.replies), before)
        await self.command("/课表")
        self.assertEqual(len(self.bot.replies), before)

    async def test_cancelled_member_lookup_does_not_send_error_or_courses(self):
        await self.import_file()
        self.bot._get_group_member_list.side_effect = asyncio.CancelledError()
        before = len(self.bot.replies)
        with self.assertRaises(asyncio.CancelledError):
            await self.command("/课ing")
        self.assertEqual(len(self.bot.replies), before)

    async def test_today_only_displays_sender_even_with_another_member_mention(self):
        await self.import_file()
        await self.import_file(OTHER_CALENDAR, user_id=222)
        await self.command("/今日课程", mentions=["222"])
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Course B", self.latest())
        self.assertNotIn("2026-09-14", self.latest())
        self.assertEqual(self.bot.replies[-1][1][0], {"type": "at", "data": {"qq": "111"}})
        await self.command("/今日课程", group_id=200)
        self.assertIn("尚未导入", self.latest())

    async def test_unimported_and_idle_query_responses_are_distinct(self):
        await self.command("/课ing")
        self.assertIn("本群当前成员尚未导入课表", self.latest())
        await self.command("/今日课程")
        self.assertIn("你尚未导入课表", self.latest())
        self.assertIn("/导入课表", self.latest())
        await self.import_file()
        with patch("plugins.timetable._now", return_value=datetime(2026, 9, 8, 10, tzinfo=SHANGHAI)):
            await self.command("/今日课程")
        self.assertIn("今日无课程", self.latest())
        self.assertNotIn("尚未导入", self.latest())

    async def test_today_query_cross_midnight_displays_both_dates(self):
        calendar = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:night\r\n"
                    "SUMMARY:Night course\r\nDTSTART;TZID=Asia/Shanghai:20260906T233000\r\n"
                    "DTEND;TZID=Asia/Shanghai:20260907T003000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode()
        await self.import_file(calendar)
        await self.command("/今日课程")
        self.assertIn("2026-09-06 23:30–2026-09-07 00:30 Night course", self.latest())

    async def test_query_text_cannot_create_cq_mentions_or_other_segments(self):
        await self.import_file(CALENDAR.replace(b"Course A", b"[CQ:at,qq=all]\\nCourse"))
        for command in ("/课ing", "/今日课程"):
            await self.command(command)
            segments = self.bot.replies[-1][1]
            self.assertEqual([s["type"] for s in segments], ["at", "text"])
            self.assertEqual(segments[0]["data"]["qq"], "111")
            self.assertIn("[CQ:at,qq=all] Course", segments[1]["data"]["text"])

    async def test_query_long_results_split_without_omitting_courses(self):
        for user in range(111, 115):
            await self.import_file(user_id=user)
        self.bot.replies.clear()
        with patch("plugins.timetable.MAX_REPLY_CHARS", 120):
            await self.command("/课ing")
        self.assertGreater(len(self.bot.replies), 1)
        messages = "\n".join(reply_text(reply) for reply in self.bot.replies)
        for user in range(111, 115):
            self.assertEqual(messages.count(f"QQ {user}："), 1)
        self.assertEqual(messages.count("Course A"), 4)
        self.assertTrue(all(len(reply_text(reply)) < 150 for reply in self.bot.replies))

    async def test_query_message_cap_refuses_before_sending_partial_results(self):
        await self.import_file()
        self.bot.replies.clear()
        with patch("plugins.timetable.MAX_REPLY_CHARS", 40), patch("plugins.timetable.MAX_QUERY_MESSAGES", 1):
            await self.command("/课ing")
        self.assertEqual(len(self.bot.replies), 1)
        self.assertIn("未发送不完整课表", self.latest())
        self.assertNotIn("Course A", self.latest())

    async def test_imported_data_survives_plugin_restart_but_pending_file_does_not(self):
        saved = await self.import_file()
        await self.prepare(OTHER_CALENDAR, update=True)
        self.plugin.close()
        self.plugin = TimetablePlugin(images_enabled=False, database_path=self.db_path)
        self.assertEqual(self.plugin.sessions, {})
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), saved)
        await self.command("/今日课程")
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Course B", self.latest())
        await self.command("/已导入")
        self.assertIn("无需重复确认", self.latest())

    async def test_upload_after_success_is_ignored_without_new_session(self):
        await self.import_file()
        self.download.reset_mock()
        before = len(self.bot.replies)
        await self.plugin.handle_notice(self.bot, None, upload_event(file_id="late-file"))
        self.download.assert_not_called()
        self.assertEqual(len(self.bot.replies), before)

    async def test_lost_success_reply_does_not_repeat_the_committed_import(self):
        await self.prepare()
        with patch.object(self.bot, "_send_reply", side_effect=ConnectionError("private-network-detail")):
            with self.assertLogs("qq-bot", level="WARNING") as logs:
                with self.assertRaises(ConnectionError):
                    await self.command("/已导入")
                await asyncio.sleep(0)
        self.assertNotIn("private-network-detail", "".join(logs.output))
        saved = await self.plugin.store.get_import(("100", "111"))
        self.assertIsNotNone(saved)
        self.assertEqual(self.plugin.sessions, {})
        await self.command("/已导入")
        self.assertIn("无需重复确认", self.latest())
        self.assertEqual(await self.plugin.store.get_import(("100", "111")), saved)
        self.parser.assert_awaited_once()

    async def test_all_new_commands_are_registered_and_remain_group_only(self):
        for command in ("/课表", "/导入课表", "/已导入", "/更新课表", "/课ing", "/今日课程", "/取消导入"):
            context = CommandContext(command, [])
            self.assertTrue(self.plugin.matches(command, context))
            self.assertIn(command, dispatch_command("/help", ("timetable",)))
            self.assertNotIn(command, dispatch_command("/help", ("basic",)))
            event = command_event(command)
            event["message_type"] = "private"
            await self.plugin.handle(self.bot, None, event, context)
            self.assertIn("仅支持群聊", self.latest())
        self.assertFalse(self.db_path.exists())


class TimetableConfigurationTest(unittest.TestCase):
    def test_explicit_plugin_selection_is_preserved_until_timetable_is_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_config.json"
            settings = {"summary": {"deepseek_api_key": "synthetic-placeholder"},
                        "auto_emoji": {"target_qq": "123"}}
            save_plugin_config(("auto_emoji", "summary", "basic"), settings, path)
            before = path.read_bytes()
            existing = load_plugin_config(path)
            self.assertNotIn("timetable", existing.enabled_plugin_ids)
            self.assertEqual(path.read_bytes(), before)
            save_plugin_config((*existing.enabled_plugin_ids, "timetable"), existing.plugin_settings, path)
            enabled = load_plugin_config(path)
            self.assertEqual(set(enabled.enabled_plugin_ids), {"auto_emoji", "summary", "basic", "timetable"})
            self.assertEqual(enabled.plugin_settings, settings)
            definition = next(item for item in PLUGIN_DEFINITIONS if item.plugin_id == "timetable")
            self.assertEqual(definition.name, "课表")
            for command in definition.commands:
                self.assertIn(command, dispatch_command("/help", enabled.enabled_plugin_ids))


class BotTimetableRoutingTest(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, enabled=("timetable", "basic")):
        config = BotConfig(
            websocket_mode="server", ws_url="ws://napcat:3001", access_token=None, bot_qq="999",
            listen_host="127.0.0.1", listen_port=8080, reconnect_seconds=1, quark_cookie=None,
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / "timetable.sqlite3"
        with patch("bot.load_plugin_config", return_value=PluginConfig(enabled, {})), patch(
            "bot.TimetablePlugin", side_effect=lambda settings=None: TimetablePlugin(settings, images_enabled=False, database_path=db_path),
        ):
            bot = NapCatBot(config)
        self.addCleanup(bot.stop)
        return bot

    async def test_factory_and_dynamic_help_registration(self):
        bot = self.make_bot()
        self.assertTrue(any(isinstance(plugin, TimetablePlugin) for plugin in bot.plugins))
        self.assertIn("/导入课表", dispatch_command("/help", bot.enabled_plugin_ids))
        self.assertNotIn("/导入课表", dispatch_command("/help", ("basic",)))

    async def test_import_requires_a_real_bot_mention(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        event = command_event("/导入课表")
        event["message"] = "@bot /导入课表"
        await bot._handle_raw_event(websocket, json.dumps(event))
        plugin = next(plugin for plugin in bot.plugins if isinstance(plugin, TimetablePlugin))
        self.assertEqual(plugin.sessions, {})
        await bot._handle_raw_event(websocket, json.dumps(command_event("/导入课表")))
        self.assertIn(("100", "111"), plugin.sessions)

    async def test_notices_do_not_reach_legacy_message_hooks(self):
        bot = self.make_bot()
        legacy = SimpleNamespace(handle_event=AsyncMock(), matches=Mock(return_value=False))
        notice_hook = SimpleNamespace(handle_notice=AsyncMock(), matches=Mock(return_value=False))
        bot.plugins.extend([legacy, notice_hook])
        await bot._handle_raw_event(FakeWebSocket(), json.dumps(upload_event()))
        legacy.handle_event.assert_not_awaited()
        notice_hook.handle_notice.assert_awaited_once()

    async def test_disabled_plugin_does_not_receive_uploads(self):
        bot = self.make_bot(("basic",))
        websocket = FakeWebSocket()
        await bot._handle_raw_event(websocket, json.dumps(upload_event()))
        self.assertEqual(websocket.sent, [])
        for command in ("/课表", "/导入课表", "/已导入", "/更新课表", "/课ing", "/今日课程", "/取消导入"):
            await bot._handle_raw_event(websocket, json.dumps(command_event(command)))
            text = websocket.sent[-1]["params"]["message"][0]["data"]["text"]
            self.assertIn("没有该指令", text)
        self.assertTrue(all(item["action"] == "send_msg" for item in websocket.sent))

    async def test_non_object_json_is_ignored(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        for raw in ("[]", "null", '"text"', "42"):
            await bot._handle_raw_event(websocket, raw)
        self.assertEqual(websocket.sent, [])

    async def test_action_echo_and_other_commands_work_while_file_is_downloading(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        await bot._handle_raw_event(websocket, json.dumps(command_event("/导入课表")))
        started = threading.Event()
        release = threading.Event()

        def blocked_download(*args):
            started.set()
            if not release.wait(5):
                raise TimeoutError()
            return inspect_calendar_payload(CALENDAR)

        with patch("plugins.timetable.download_calendar_file", side_effect=blocked_download):
            task = asyncio.create_task(bot._handle_raw_event(websocket, json.dumps(upload_event())))
            try:
                await asyncio.wait_for(websocket.file_action_sent.wait(), 2)
                request = next(data for data in websocket.sent if data["action"] == "get_group_file_url")
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(command_event("/hello"))), 1)
                await bot._handle_raw_event(websocket, json.dumps({
                    "echo": request["echo"], "status": "ok", "retcode": 0, "data": {"url": DOWNLOAD_URL},
                }))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(command_event("/hello"))), 1)
                future = asyncio.get_running_loop().create_future()
                bot._action_waiters["other-api-echo"] = future
                await bot._handle_raw_event(websocket, json.dumps({"echo": "other-api-echo", "data": {"ok": True}}))
                self.assertTrue(future.result()["data"]["ok"])
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        messages = [data["params"]["message"] for data in websocket.sent if data["action"] == "send_msg"]
        texts = ["".join(part["data"]["text"] for part in message if part["type"] == "text") for message in messages]
        self.assertEqual(texts.count("啦啦啦"), 2)
        self.assertEqual(sum("文件已接收：" in text for text in texts), 1)
        self.assertEqual(bot._action_waiters, {})

    async def test_current_course_member_action_round_trip_keeps_other_commands_responsive(self):
        bot = self.make_bot()
        plugin = next(plugin for plugin in bot.plugins if isinstance(plugin, TimetablePlugin))
        await plugin.store.save(("100", "111"), "test.ics", "a" * 64, parse_calendar(CALENDAR))
        await plugin.store.save(("100", "222"), "test.ics", "b" * 64, parse_calendar(OTHER_CALENDAR))
        websocket = FakeWebSocket()
        with patch("plugins.timetable._now", return_value=datetime(2026, 9, 7, 10, tzinfo=SHANGHAI)):
            task = asyncio.create_task(bot._handle_raw_event(websocket, json.dumps(command_event("/课ing", user_id=333))))
            try:
                await asyncio.wait_for(websocket.member_action_sent.wait(), 1)
                request = next(item for item in websocket.sent if item["action"] == "get_group_member_list")
                self.assertEqual(request["params"], {"group_id": 100})
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(command_event("/hello"))), 1)
                await bot._handle_raw_event(websocket, json.dumps({
                    "echo": request["echo"], "status": "ok", "retcode": 0,
                    "data": [{"group_id": 100, "user_id": 111, "card": "真实消息段昵称"}, {"user_id": 333}],
                }))
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        messages = [item["params"]["message"] for item in websocket.sent if item["action"] == "send_msg"]
        texts = ["".join(part["data"]["text"] for part in message if part["type"] == "text") for message in messages]
        self.assertIn("啦啦啦", texts)
        self.assertIn("真实消息段昵称：", texts[-1])
        self.assertIn("Course A", texts[-1])
        self.assertNotIn("Course B", texts[-1])
        self.assertEqual([part["data"]["qq"] for part in messages[-1] if part["type"] == "at"], ["333"])
        self.assertEqual(bot._action_waiters, {})

    async def test_member_action_error_echo_does_not_expose_stored_courses(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        task = asyncio.create_task(bot._handle_raw_event(websocket, json.dumps(command_event("/课ing"))))
        try:
            await asyncio.wait_for(websocket.member_action_sent.wait(), 1)
            request = next(item for item in websocket.sent if item["action"] == "get_group_member_list")
            with self.assertLogs("qq-bot", level="WARNING"):
                await bot._handle_raw_event(websocket, json.dumps({
                    "echo": request["echo"], "status": "failed", "retcode": 1200,
                }))
                await asyncio.wait_for(task, 2)
            message = websocket.sent[-1]["params"]["message"]
            self.assertIn("获取群成员列表失败", message[1]["data"]["text"])
            self.assertEqual(bot._action_waiters, {})
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_bot_stop_clears_timetable_sessions(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        await bot._handle_raw_event(websocket, json.dumps(command_event("/导入课表")))
        plugin = next(plugin for plugin in bot.plugins if isinstance(plugin, TimetablePlugin))
        session = plugin.sessions[("100", "111")]
        bot.stop()
        self.assertTrue(session.expiry_handle.cancelled())
        self.assertEqual(plugin.sessions, {})


if __name__ == "__main__":
    unittest.main()
