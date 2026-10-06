"""Image delivery uses real Pillow, synthetic rows and a local fake OneBot peer."""
import asyncio
import base64
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from bot import BotConfig, NapCatBot
from plugin_control import PluginConfig
from plugins.common import CommandContext, OneBotActionError, image_segment_from_bytes
from plugins.timetable import TimetablePlugin, _image_reply_confirmed, _image_segment
from plugins.timetable_avatar import AvatarCache
from plugins.timetable_ics import CourseOccurrence, ParsedTimetable, SHANGHAI
from plugins.timetable_images import TimetableImageBusy, TimetableImageService
from plugins.timetable_render import RenderLimits, TimetableRenderError
from plugins.timetable_store import MemberCourses, TimetableStore, TimetableStoreError

NOW = datetime(2026, 9, 8, 10, 20, tzinfo=SHANGHAI)
LESSON = CourseOccurrence("synthetic", NOW.replace(hour=9, minute=55), "示例课程",
                          NOW.replace(hour=9, minute=55), NOW.replace(hour=11, minute=20), "教室 A")


def text(message):
    return "".join(item["data"]["text"] for item in message if item["type"] == "text")


class FakeImageBot:
    def __init__(self):
        self.replies = []
        self.outcomes = []
        self.text_error = None
        self._get_group_member_list = AsyncMock(return_value=[
            {"group_id": 100, "user_id": 111, "card": "示例甲"},
            {"group_id": 100, "user_id": 222, "nickname": "示例乙"},
        ])

    async def _send_reply(self, websocket, event, message, *, wait_for_response=False, timeout=12):
        self.replies.append((event, message, wait_for_response))
        if wait_for_response:
            result = self.outcomes.pop(0) if self.outcomes else {
                "status": "ok", "retcode": 0, "data": {"message_id": -123},
            }
            if isinstance(result, BaseException):
                raise result
            return result
        if self.text_error:
            raise self.text_error

    @property
    def images(self):
        return [entry for entry in self.replies if entry[2]]

    @property
    def words(self):
        return "\n".join(text(entry[1]) for entry in self.replies if not entry[2])


class ImageWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.avatar_fetch = Mock(return_value=None)
        self.service = TimetableImageService(avatars=AvatarCache(fetch=self.avatar_fetch))
        self.service.plan = AsyncMock(wraps=self.service.plan)
        self.plugin = TimetablePlugin(database_path=self.directory / "timetable.sqlite3", image_service=self.service)
        self.addCleanup(self.plugin.close)
        self.plugin.store.current_courses = AsyncMock(return_value=(MemberCourses("111", (LESSON,)), MemberCourses("222", ())))
        self.plugin.store.today_courses = AsyncMock(return_value=(LESSON,))
        self.bot = FakeImageBot()
        self.clock = Mock(return_value=NOW)
        clock_patch = patch("plugins.timetable._now", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    async def command(self, command="/课ing", *, user=111, mentions=(), sender=None):
        event = {"message_type": "group", "group_id": 100, "user_id": user,
                 "sender": sender or {"user_id": user, "card": "自己的群名片"}}
        await self.plugin.handle(self.bot, None, event, CommandContext(command, list(mentions)))

    def last_view(self):
        return self.service.plan.await_args.args[0]

    def raw_image(self, index=-1):
        message = self.bot.images[index][1]
        image = next(item for item in message if item["type"] == "image")
        self.assertTrue(image["data"]["file"].startswith("base64://"))
        return base64.b64decode(image["data"]["file"][9:])

    def many_members(self, count=6):
        self.bot._get_group_member_list.return_value = [{"user_id": i, "card": f"成员{i}"} for i in range(111, 111 + count)]
        self.plugin.store.current_courses.return_value = tuple(MemberCourses(str(i), (LESSON,)) for i in range(111, 111 + count))

    async def test_current_reply_is_memory_png_plus_real_requester_mention(self):
        with patch.object(Path, "write_bytes", side_effect=AssertionError("Runtime image disk write")), patch.object(
            Path, "write_text", side_effect=AssertionError("Runtime image disk write"),
        ):
            await self.command()
        self.assertEqual(len(self.bot.images), 1)
        event, message, checked = self.bot.images[0]
        self.assertEqual(event["group_id"], 100)
        self.assertEqual([item["type"] for item in message], ["at", "text", "image"])
        self.assertEqual(message[0]["data"]["qq"], "111")
        self.assertTrue(checked)
        self.assertEqual([m.name for m in self.last_view().members], ["示例甲", "示例乙"])
        self.assertEqual(self.last_view().members[1].courses, ())
        with Image.open(io.BytesIO(self.raw_image())) as image:
            self.assertEqual(image.width, 1560)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.bot.words, "")

    async def test_tomorrow_image_keeps_real_clock_and_next_day(self):
        self.clock.return_value = NOW.replace(month=12, day=31)
        await self.command("/明日课程", mentions=("222",))
        view = self.last_view()
        self.assertEqual(view.mode, "tomorrow")
        self.assertEqual(view.now, self.clock.return_value)
        self.assertEqual(view.target_day.date().isoformat(), "2027-01-01")
        self.assertEqual([m.user_id for m in view.members], ["111"])
        self.bot._get_group_member_list.assert_not_awaited()
        for call in self.plugin.store.today_courses.await_args_list:
            self.assertEqual(call.args, (("100", "111"), view.target_day))
        self.assertTrue(self.raw_image().startswith(b"\x89PNG"))
        self.assertEqual(self.bot.images[-1][1][0]["data"]["qq"], "111")

    async def test_tomorrow_empty_render_failure_uses_tomorrow_text(self):
        self.plugin.store.today_courses.return_value = ()
        self.service.plan.side_effect = TimetableRenderError("test")
        await self.command("/明日课程")
        self.assertIn("你的明日课程（2026-09-09", self.bot.words)
        self.assertIn("明日无课程", self.bot.words)

    async def test_today_only_uses_sender_despite_other_mentions(self):
        await self.command("/今日课程", mentions=("222",))
        view = self.last_view()
        self.assertEqual(view.mode, "today")
        self.assertEqual([(m.user_id, m.name) for m in view.members], [("111", "自己的群名片")])
        self.bot._get_group_member_list.assert_not_awaited()
        for args in self.plugin.store.today_courses.await_args_list:
            self.assertEqual(args.args[0], ("100", "111"))
        self.avatar_fetch.assert_called_once_with("111")

    async def test_sender_identity_mismatch_does_not_use_other_persons_name(self):
        await self.command("/今日课程", sender={"user_id": 222, "card": "其他人的名字"})
        self.assertEqual(self.last_view().members[0].name, "QQ 111")

    async def test_duplicate_group_names_remain_distinguishable(self):
        self.bot._get_group_member_list.return_value = [{"user_id": user, "card": "同名"} for user in (111, 222)]
        await self.command()
        self.assertEqual([m.name for m in self.last_view().members], ["同名（QQ 111）", "同名（QQ 222）"])

    async def test_each_current_query_refreshes_allowlist_and_names(self):
        await self.command()
        self.bot._get_group_member_list.return_value = [{"user_id": 111, "card": "新群名片"}]
        self.plugin.store.current_courses.return_value = (MemberCourses("111", (LESSON,)),)
        await self.command()
        self.assertEqual(self.bot._get_group_member_list.await_count, 2)
        self.assertEqual(self.plugin.store.current_courses.await_args.kwargs["user_ids"], ("111",))
        self.assertEqual(self.last_view().members[0].name, "新群名片")
        self.assertEqual(len(self.last_view().members), 1)

    async def test_membership_failure_never_uses_cached_or_unfiltered_data(self):
        self.bot._get_group_member_list.side_effect = OSError("private-url")
        with self.assertLogs("qq-bot", level="WARNING") as logs:
            await self.command()
        self.assertIn("获取群成员列表失败", self.bot.words)
        self.assertNotIn("private-url", "".join(logs.output))
        self.plugin.store.current_courses.assert_not_awaited()
        self.assertEqual(self.bot.images, [])

    async def test_unimported_uses_guide_without_avatar_or_render_work(self):
        self.plugin.store.today_courses.return_value = None
        await self.command("/今日课程")
        self.assertIn("你尚未导入课表", self.bot.words)
        self.assertIn("/导入课表", self.bot.words)
        self.plugin.store.current_courses.return_value = ()
        await self.command()
        self.assertIn("本群当前成员尚未导入课表", self.bot.words)
        self.avatar_fetch.assert_not_called()
        self.service.plan.assert_not_awaited()

    async def test_imported_empty_day_is_an_image_not_unimported(self):
        self.plugin.store.today_courses.return_value = ()
        await self.command("/今日课程")
        self.assertEqual(len(self.bot.images), 1)
        self.assertEqual(self.last_view().members[0].courses, ())
        self.assertNotIn("未导入", self.bot.words)

    async def test_query_time_refreshes_after_avatar_wait_at_course_boundaries(self):
        afternoon = replace(LESSON, starts_at=NOW.replace(hour=14, minute=30), ends_at=NOW.replace(hour=15, minute=55))
        self.plugin.store.current_courses.side_effect = lambda group, now, **kw: (
            MemberCourses("111", (afternoon,) if afternoon.starts_at <= now < afternoon.ends_at else ()),
        )
        for moment, active in [(afternoon.starts_at, True), (afternoon.ends_at, False)]:
            self.clock.return_value = moment - timedelta(seconds=1)
            async def wait_for_avatar(ids):
                self.clock.return_value = moment
                return {}
            with patch.object(self.service.avatars, "get_many", side_effect=wait_for_avatar):
                await self.command()
            self.assertEqual(self.last_view().now, moment)
            self.assertEqual(bool(self.last_view().members[0].courses), active)
        self.assertNotEqual(self.raw_image(0), self.raw_image(1))

    async def test_today_refreshes_after_midnight(self):
        self.clock.return_value = NOW.replace(hour=23, minute=59, second=59)
        midnight = (NOW + timedelta(days=1)).replace(hour=0, minute=0)
        async def avatars(ids):
            self.clock.return_value = midnight
            return {}
        self.plugin.store.today_courses.side_effect = lambda key, now: (LESSON,) if now.date() == NOW.date() else ()
        with patch.object(self.service.avatars, "get_many", side_effect=avatars):
            await self.command("/今日课程")
        self.assertEqual(self.last_view().now, midnight)
        self.assertEqual(self.last_view().members[0].courses, ())

    async def test_tomorrow_refreshes_target_day_after_midnight(self):
        self.clock.return_value = NOW.replace(hour=23, minute=59, second=59)
        midnight = (NOW + timedelta(days=1)).replace(hour=0, minute=0)
        async def avatars(ids):
            self.clock.return_value = midnight
            return {}
        with patch.object(self.service.avatars, "get_many", side_effect=avatars):
            await self.command("/明日课程")
        self.assertEqual(self.last_view().now, midnight)
        self.assertEqual(self.last_view().target_day.date(), (NOW + timedelta(days=2)).date())
        self.assertEqual([call.args[1].date() for call in self.plugin.store.today_courses.await_args_list],
                         [(NOW + timedelta(days=1)).date(), (NOW + timedelta(days=2)).date()])

    async def test_missing_font_and_queue_busy_fall_back_to_complete_text(self):
        for error in (TimetableRenderError("private-font"), TimetableImageBusy("busy")):
            with self.subTest(error=type(error)), patch.object(self.service, "plan", side_effect=error), self.assertLogs("qq-bot", level="WARNING") as logs:
                await self.command()
            self.assertNotIn("private-font", "".join(logs.output))
        self.assertEqual(self.bot.images, [])
        self.assertIn("示例甲：", self.bot.words)
        self.assertIn("示例课程", self.bot.words)
        self.assertIn("示例乙：无课程", self.bot.words)

    async def test_page_limit_falls_back_before_any_partial_image(self):
        self.many_members()
        self.service.limits = RenderLimits(max_pages=1)
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command()
        self.assertEqual(self.bot.images, [])
        for user in range(111, 117):
            self.assertIn(f"成员{user}：", self.bot.words)

    async def test_multi_page_sequential_send_has_identity_and_receipts(self):
        self.many_members()
        await self.command()
        self.assertEqual(len(self.bot.images), 2)
        for number, (event, message, checked) in enumerate(self.bot.images, 1):
            self.assertEqual(message[0], {"type": "at", "data": {"qq": "111"}})
            self.assertIn(f"{number}/2", text(message))
            self.assertTrue(checked)
        self.assertEqual(self.bot.words, "")

    async def test_explicit_rejection_uses_complete_text_without_image_retry(self):
        self.bot.outcomes = [OneBotActionError("not logged")]
        await self.command()
        self.assertEqual(len(self.bot.images), 1)
        self.assertIn("图片发送被拒绝", self.bot.words)
        self.assertIn("示例课程", self.bot.words)

    async def test_partial_rejection_and_partial_render_failure_report_full_text(self):
        self.many_members()
        self.bot.outcomes = [{"status": "ok", "retcode": 0, "data": {"message_id": 1}}, OneBotActionError()]
        await self.command()
        self.assertIn("图片仅确认发送 1/2 页", self.bot.words)
        for user in range(111, 117):
            self.assertIn(f"成员{user}：", self.bot.words)
        self.bot.replies.clear()
        original = self.service.work
        async def fail_second(function, *args):
            if function is _image_segment and args[1] == 1:
                raise TimetableRenderError("synthetic failure")
            return await original(function, *args)
        with patch.object(self.service, "work", side_effect=fail_second), self.assertLogs("qq-bot", level="WARNING"):
            await self.command()
        self.assertEqual(len(self.bot.images), 1)
        self.assertIn("图片仅确认发送 1/2 页", self.bot.words)

    async def test_unknown_ack_or_transport_does_not_blindly_resend(self):
        outcomes = [None, {}, {"status": "async", "retcode": 1},
                    {"status": "ok", "retcode": 0, "data": {}}, TimeoutError("private"), ConnectionError("private")]
        self.many_members()
        for outcome in outcomes:
            self.bot.replies.clear()
            self.bot.outcomes = [outcome]
            with self.subTest(outcome=type(outcome).__name__), patch("plugins.timetable.LOGGER.warning"):
                await self.command()
            self.assertEqual(len(self.bot.images), 1)
            self.assertIn("未能确认", self.bot.words)
            self.assertIn("不自动重发", self.bot.words)
            self.assertNotIn("示例课程", self.bot.words)

    async def test_unknown_send_and_failed_notice_never_trigger_full_text_retry(self):
        self.bot.outcomes = [TimeoutError("private")]
        self.bot.text_error = ConnectionError("private")
        with self.assertLogs("qq-bot", level="WARNING") as logs:
            await self.command()
        self.assertEqual(len(self.bot.replies), 2)
        self.assertNotIn("示例课程", self.bot.words)
        self.assertNotIn("private", "".join(logs.output))

    async def test_stalled_unconfirmed_notice_cannot_hold_image_slot_forever(self):
        async def stalled_send(websocket, event, message, *, wait_for_response=False, timeout=12):
            if wait_for_response:
                raise TimeoutError()
            await asyncio.Event().wait()
        with patch.object(self.bot, "_send_reply", side_effect=stalled_send) as send, patch(
            "plugins.timetable.IMAGE_NOTICE_SECONDS", .03,
        ), self.assertLogs("qq-bot", level="WARNING"):
            await asyncio.wait_for(self.command(), 2)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(self.service._requests, set())

    async def test_cancel_during_send_or_avatar_propagates_without_fallback(self):
        self.bot.outcomes = [asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.command()
        self.assertEqual(self.bot.words, "")
        with patch.object(self.service.avatars, "get_many", side_effect=asyncio.CancelledError()), self.assertRaises(asyncio.CancelledError):
            await self.command()
        self.assertEqual(self.service._requests, set())

    async def test_close_during_avatar_wait_sends_nothing(self):
        async def stop(ids):
            self.plugin.close()
            return {}
        with patch.object(self.service.avatars, "get_many", side_effect=stop):
            await self.command()
        self.assertEqual(self.bot.replies, [])
        self.service.plan.assert_not_awaited()

    async def test_failed_avatar_fetch_still_renders_default_avatar_image(self):
        self.avatar_fetch.side_effect = OSError("synthetic avatar outage")
        await self.command()
        self.assertEqual(len(self.bot.images), 1)
        self.assertEqual(self.bot.words, "")

    async def test_close_after_render_before_send_drops_result(self):
        original = self.service.work
        async def stop_after_render(function, *args):
            result = await original(function, *args)
            if function is _image_segment:
                self.plugin.close()
            return result
        with patch.object(self.service, "work", side_effect=stop_after_render):
            await self.command()
        self.assertEqual(self.bot.replies, [])

    async def test_database_refresh_failure_does_not_send_old_snapshot(self):
        self.plugin.store.today_courses.side_effect = [(LESSON,), TimetableStoreError("课表数据库暂时不可用。")]
        await self.command("/今日课程")
        self.assertEqual(self.bot.images, [])
        self.assertIn("课表数据库暂时不可用", self.bot.words)
        self.assertNotIn("示例课程", self.bot.words)

    async def test_existing_database_and_updates_need_no_reimport_or_schema_change(self):
        self.plugin.store.close()
        self.plugin.store = TimetableStore(self.directory / "real.sqlite3")
        parsed = ParsedTimetable(1, 1, (LESSON,))
        old = await self.plugin.store.save(("100", "111"), "test.ics", "a" * 64, parsed)
        await self.plugin.store.save(("200", "111"), "other.ics", "c" * 64,
                                     replace(parsed, occurrences=(replace(LESSON, name="其他群秘密课程"),)))
        await self.plugin.store.save(("100", "333"), "departed.ics", "d" * 64, parsed)
        await self.command()
        self.assertEqual([m.user_id for m in self.last_view().members], ["111"])
        self.assertEqual(self.last_view().members[0].courses[0].name, "示例课程")
        updated = replace(parsed, occurrences=(replace(LESSON, name="更新后的课程"),))
        await self.plugin.store.save(("100", "111"), "new.ics", "b" * 64, updated, expected_revision=old.revision)
        await self.command("/今日课程")
        self.assertEqual(self.last_view().members[0].courses[0].name, "更新后的课程")
        self.assertNotEqual(self.raw_image(0), self.raw_image(1))
        self.assertEqual({path.suffix for path in self.directory.iterdir()}, {".sqlite3"})


class ReplyReceiptTest(unittest.TestCase):
    def test_bytes_helper_preserves_payload(self):
        raw = b"\x89PNG\x00synthetic"
        self.assertEqual(image_segment_from_bytes(raw), {"type": "image", "data": {"file": "base64://" + base64.b64encode(raw).decode()}})

    def test_receipt_requires_success_and_numeric_not_bool_message_id(self):
        for identity in (1, -1, 0, "1", "-1"):
            self.assertTrue(_image_reply_confirmed({"status": "ok", "retcode": 0, "data": {"message_id": identity}}))
        for identity in (None, True, [], {}, "url", ""):
            self.assertFalse(_image_reply_confirmed({"status": "ok", "retcode": 0, "data": {"message_id": identity}}))
        for response in ({}, None, {"status": "async", "retcode": 1}, {"status": "ok", "retcode": False, "data": {"message_id": 1}}):
            self.assertFalse(_image_reply_confirmed(response))


class CheckedReplyRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = BotConfig(websocket_mode="server", ws_url="ws://napcat:3001", access_token=None, bot_qq="999",
                           listen_host="127.0.0.1", listen_port=8080, reconnect_seconds=1, quark_cookie=None)
        with patch("bot.load_plugin_config", return_value=PluginConfig(("basic",), {})):
            self.bot = NapCatBot(config)
        self.addCleanup(self.bot.stop)
        self.event = {"message_type": "group", "group_id": 100, "user_id": 111}
        self.sent = []
        self.ready = asyncio.Event()
        async def send(raw):
            self.sent.append(json.loads(raw))
            self.ready.set()
        self.socket = SimpleNamespace(send=send)

    async def start_reply(self):
        task = asyncio.create_task(self.bot._send_reply(self.socket, self.event, "test", wait_for_response=True))
        await asyncio.wait_for(self.ready.wait(), 2)
        return task, self.sent[-1]

    async def test_echo_roundtrip_returns_receipt_and_allows_other_commands(self):
        task, request = await self.start_reply()
        self.assertFalse(task.done())
        await self.bot._send_reply(self.socket, self.event, "another plugin")
        self.assertEqual(len(self.sent), 2)
        receipt = {"echo": request["echo"], "status": "ok", "retcode": 0, "data": {"message_id": -123}}
        await self.bot._handle_raw_event(self.socket, json.dumps(receipt))
        self.assertEqual(await task, receipt)
        self.assertEqual(self.bot._action_waiters, {})
        self.assertEqual(request["params"]["group_id"], 100)

    async def test_real_command_dispatch_and_image_echo_roundtrip(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = TimetableImageService(avatars=AvatarCache(fetch=lambda _: None))
        plugin = TimetablePlugin(database_path=Path(directory.name) / "test.sqlite3", image_service=service)
        plugin.store.today_courses = AsyncMock(return_value=(LESSON,))
        with patch("bot.load_plugin_config", return_value=PluginConfig(("timetable",), {})), patch(
            "bot.TimetablePlugin", return_value=plugin,
        ):
            bot = NapCatBot(self.bot.config)
        self.addCleanup(bot.stop)
        event = {**self.event, "post_type": "message", "self_id": 999,
                 "message": [{"type": "at", "data": {"qq": "999"}},
                             {"type": "text", "data": {"text": " /今日课程"}}]}
        with patch("plugins.timetable._now", return_value=NOW):
            task = asyncio.create_task(bot._handle_raw_event(self.socket, json.dumps(event)))
            await asyncio.wait_for(self.ready.wait(), 3)
            request = self.sent[-1]
            self.assertEqual(request["action"], "send_msg")
            message = request["params"]["message"]
            self.assertEqual(message[0], {"type": "at", "data": {"qq": "111"}})
            self.assertTrue(message[-1]["data"]["file"].startswith("base64://iVBOR"))
            await bot._handle_raw_event(self.socket, json.dumps({"echo": request["echo"], "status": "ok",
                                                               "retcode": 0, "data": {"message_id": -1}}))
            await asyncio.wait_for(task, 2)
        self.assertEqual(bot._action_waiters, {})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(list(Path(directory.name).iterdir()), [])

    async def test_explicit_error_has_distinct_exception(self):
        task, request = await self.start_reply()
        await self.bot._handle_raw_event(self.socket, json.dumps({"echo": request["echo"], "status": "failed", "retcode": 1200}))
        with self.assertRaises(OneBotActionError):
            await task
        self.assertEqual(self.bot._action_waiters, {})

    async def test_async_acceptance_is_not_misclassified_as_rejection(self):
        task, request = await self.start_reply()
        receipt = {"echo": request["echo"], "status": "async", "retcode": 1}
        await self.bot._handle_raw_event(self.socket, json.dumps(receipt))
        self.assertEqual(await task, receipt)

    async def test_timeout_and_cancellation_clear_waiter_without_retry(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.bot._send_reply(self.socket, self.event, "test", wait_for_response=True, timeout=.02)
        self.assertEqual(self.bot._action_waiters, {})
        self.assertEqual(len(self.sent), 1)
        self.ready.clear()
        task, request = await self.start_reply()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.bot._action_waiters, {})
        self.assertEqual(len(self.sent), 2)

    async def test_timeout_also_bounds_stalled_socket_send(self):
        socket = SimpleNamespace(send=AsyncMock(side_effect=lambda raw: None))
        async def stall(raw):
            await asyncio.Event().wait()
        socket.send.side_effect = stall
        with self.assertRaises(asyncio.TimeoutError):
            await self.bot._send_reply(socket, self.event, "test", wait_for_response=True, timeout=.02)
        self.assertEqual(self.bot._action_waiters, {})


if __name__ == "__main__":
    unittest.main()