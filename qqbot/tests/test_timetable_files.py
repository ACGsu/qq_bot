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

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none", command_start={"/"})

from infrastructure.plugin_config import load_plugin_config, save_plugin_config
from services.plugin_catalog import PLUGIN_DEFINITIONS
from plugins.basic import dispatch_command
from services.timetable.models import TimetableCommand as CommandContext
from plugins.timetable import TimetablePlugin
from services.timetable.ics import SHANGHAI, TimetableParseError, parse_calendar
from infrastructure.timetable.store import TimetableStore, TimetableStoreError
from infrastructure.timetable.files import (
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
        with patch("infrastructure.timetable.files.urllib.request.build_opener") as build:
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
        with patch("infrastructure.timetable.files.urllib.request.build_opener") as build:
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
        with patch("infrastructure.timetable.files.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimetableFileError):
                self.fetch()

    def test_download_errors_do_not_expose_signed_url(self):
        with patch("infrastructure.timetable.files.urllib.request.build_opener") as build:
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



class TimetableConfigurationTest(unittest.TestCase):
    def test_explicit_plugin_selection_is_preserved_until_timetable_is_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_config.json"
            settings = {"auto_emoji": {"target_qq": "123"}}
            save_plugin_config(("auto_emoji", "summary", "basic"), settings, path)
            before = path.read_bytes()
            existing = load_plugin_config(path)
            self.assertNotIn("timetable", existing.enabled_plugin_ids)
            self.assertEqual(path.read_bytes(), before)
            save_plugin_config((*existing.enabled_plugin_ids, "timetable"), existing.plugin_settings, path)
            enabled = load_plugin_config(path)
            self.assertEqual(set(enabled.enabled_plugin_ids), {"auto_emoji", "basic", "timetable"})
            self.assertEqual(enabled.plugin_settings, settings)
            definition = next(item for item in PLUGIN_DEFINITIONS if item.plugin_id == "timetable")
            self.assertEqual(definition.name, "课表")
            for command in definition.commands:
                self.assertIn(command, dispatch_command("/help", enabled.enabled_plugin_ids))


