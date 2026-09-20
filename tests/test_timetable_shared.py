"""Cross-group timetable tests use synthetic identities, calendars and temporary DBs."""
import asyncio
import base64
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from plugins.common import CommandContext
from plugins.timetable import TimetablePlugin
from plugins.timetable_avatar import AvatarCache
from plugins.timetable_files import inspect_calendar_payload
from plugins.timetable_ics import CourseOccurrence, ParsedTimetable, SHANGHAI, TimetableParseError, parse_calendar
from plugins.timetable_images import TimetableImageService
from plugins.timetable_store import StoredTimetable, TimetableConflictError, TimetableStore, TimetableStoreError

NOW = datetime(2026, 9, 7, 10, 20, tzinfo=SHANGHAI)
KEY = ("100", "111")
OTHER_GROUP = ("200", "111")
SETTINGS = {"group_isolation_enabled": "false"}
CALENDAR = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:synthetic\r\n"
    "SUMMARY:Course A\r\nDTSTART;TZID=Asia/Shanghai:20260907T100000\r\n"
    "DTEND;TZID=Asia/Shanghai:20260907T110000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
).encode("utf-8")


def course(name="Course A", *, start=None, uid="synthetic"):
    start = start or NOW.replace(minute=0)
    return CourseOccurrence(uid, start, name, start, start + timedelta(hours=1), "Room 1")


def parsed(*courses):
    courses = courses or (course(),)
    return ParsedTimetable(len(courses), len({item.uid for item in courses}), courses)


def text(message):
    return message if isinstance(message, str) else "".join(
        item["data"]["text"] for item in message if item["type"] == "text"
    )


class SharedStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "timetable.sqlite3"
        self.local = TimetableStore(self.path)
        self.shared = TimetableStore(self.path, group_isolation_enabled=False)
        self.addCleanup(self.local.close)
        self.addCleanup(self.shared.close)

    async def save(self, store, key=KEY, name="Course A", *, at=NOW, revision=None, courses=None):
        with patch("plugins.timetable_store.datetime", wraps=datetime) as clock:
            clock.now.return_value = at
            return await store.save(key, "synthetic.ics", "a" * 64,
                                    parsed(*(courses or (course(name),))), expected_revision=revision)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return (
                connection.execute("PRAGMA user_version").fetchone(),
                connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall(),
                connection.execute("SELECT * FROM timetable_imports ORDER BY group_id,user_id").fetchall(),
                connection.execute("SELECT * FROM timetable_occurrences ORDER BY group_id,user_id,uid,recurrence_id").fetchall(),
            )

    async def test_old_import_is_shared_without_migration_or_reimport(self):
        original = await self.save(self.local)
        before = self.snapshot()
        for group in ("100", "200", "300"):
            self.assertEqual(await self.shared.get_import((group, "111")), original)
            self.assertEqual(await self.shared.today_courses((group, "111"), NOW), (course(),))
        self.assertIsNone(await self.local.get_import(OTHER_GROUP))
        self.assertEqual(before, self.snapshot())

    async def test_first_shared_import_keeps_its_source_group_and_rejects_duplicates(self):
        original = await self.save(self.shared)
        with self.assertRaises(TimetableConflictError):
            await self.save(self.shared, OTHER_GROUP, "Must not overwrite")
        self.assertEqual(await self.shared.get_import(OTHER_GROUP), original)
        self.assertEqual(await self.local.get_import(KEY), original)
        self.assertIsNone(await self.local.get_import(OTHER_GROUP))

    async def test_different_qq_accounts_never_share_ownership(self):
        await self.save(self.shared)
        self.assertIsNone(await self.shared.get_import(("200", "222")))
        self.assertIsNone(await self.shared.today_courses(("200", "222"), NOW))
        await self.save(self.shared, ("200", "222"), "Member B")
        self.assertEqual([c.name for c in await self.shared.today_courses(("300", "222"), NOW)], ["Member B"])
        self.assertEqual([c.name for c in await self.shared.today_courses(OTHER_GROUP, NOW)], ["Course A"])

    async def test_latest_whole_version_wins_including_over_an_older_local_record(self):
        await self.save(self.local, name="Old local")
        newest = await self.save(self.local, OTHER_GROUP, "Latest shared", at=NOW + timedelta(seconds=1))
        for key in (KEY, OTHER_GROUP, ("300", "111")):
            self.assertEqual(await self.shared.get_import(key), newest)
            self.assertEqual([c.name for c in await self.shared.today_courses(key, NOW)], ["Latest shared"])
        members = await self.shared.current_courses("100", NOW, user_ids=("111",))
        self.assertEqual([(m.user_id, [c.name for c in m.courses]) for m in members], [("111", ["Latest shared"])])

    async def test_newest_version_with_no_courses_today_does_not_fall_back_to_old_courses(self):
        await self.save(self.local, name="Old active course")
        await self.save(self.local, OTHER_GROUP, at=NOW + timedelta(seconds=1),
                        courses=(course("Tomorrow", start=NOW + timedelta(days=1)),))
        self.assertEqual(await self.shared.today_courses(KEY, NOW), ())
        members = await self.shared.current_courses("300", NOW, user_ids=("111",))
        self.assertEqual([(m.user_id, m.courses) for m in members], [("111", ())])

    async def test_second_precision_ties_are_stable_across_queries_and_updates(self):
        await self.save(self.local, OTHER_GROUP, "Stable winner")
        await self.save(self.local, KEY, "Other legacy record")
        self.assertEqual((await self.shared.get_import(KEY)).group_id, "200")
        chosen = await self.shared.get_import(OTHER_GROUP)
        updated = await self.save(self.shared, ("300", "111"), "Updated winner", revision=chosen.revision)
        self.assertEqual(updated.group_id, "200")
        for key in (KEY, OTHER_GROUP, ("300", "111")):
            self.assertEqual(await self.shared.get_import(key), updated)

    async def test_current_courses_filters_to_present_members_and_preserves_idle_members(self):
        await self.save(self.local)
        await self.save(self.local, ("300", "222"), courses=(course("Tomorrow", start=NOW + timedelta(days=1)),))
        await self.save(self.local, ("100", "333"), "Not in requesting group")
        with patch("plugins.timetable_store.MAX_QUERY_ROWS", 2):
            members = await self.shared.current_courses("200", NOW, user_ids=("222", "111", "111"))
        self.assertEqual([m.user_id for m in members], ["111", "222"])
        self.assertEqual(members[1].courses, ())
        self.assertNotIn("333", [m.user_id for m in members])

    async def test_shared_group_query_requires_an_explicit_member_allowlist(self):
        await self.save(self.shared)
        with self.assertRaises(TimetableStoreError):
            await self.shared.current_courses("200", NOW)
        self.assertEqual(await self.shared.current_courses("200", NOW, user_ids=()), ())
        self.assertEqual(await self.shared.current_courses("200", NOW, user_ids=("999",)), ())

    async def test_query_limit_applies_after_version_selection_without_silent_truncation(self):
        await self.save(self.local)
        await self.save(self.local, OTHER_GROUP, "Latest", at=NOW + timedelta(seconds=1))
        with patch("plugins.timetable_store.MAX_QUERY_ROWS", 1):
            result = await self.shared.current_courses("300", NOW, user_ids=("111",))
            self.assertEqual(len(result), 1)
            await self.save(self.shared, ("200", "222"))
            with self.assertRaises(TimetableStoreError):
                await self.shared.current_courses("300", NOW, user_ids=("111", "222"))

    async def test_large_member_allowlist_does_not_use_one_large_sql_binding_list(self):
        await self.save(self.shared)
        members = await self.shared.current_courses("200", NOW, user_ids=(str(i) for i in range(1, 2001)))
        self.assertEqual([m.user_id for m in members], ["111"])

    async def test_shared_update_retains_origin_and_reenabling_preserves_other_group_records(self):
        original = await self.save(self.local, name="Group A retained")
        selected = await self.save(self.local, OTHER_GROUP, "Group B selected", at=NOW + timedelta(seconds=1))
        updated = await self.save(self.shared, ("300", "111"), "Shared replacement",
                                  at=NOW + timedelta(seconds=2), revision=selected.revision)
        self.assertEqual(updated.group_id, "200")
        self.assertEqual(updated.imported_at, selected.imported_at)
        self.assertNotEqual(updated.revision, selected.revision)
        self.assertEqual(await self.shared.get_import(KEY), updated)
        self.assertEqual(await self.local.get_import(KEY), original)
        self.assertEqual(await self.local.get_import(OTHER_GROUP), updated)
        self.assertIsNone(await self.local.get_import(("300", "111")))
        self.assertEqual([c.name for c in await self.local.today_courses(KEY, NOW)], ["Group A retained"])
        self.assertEqual(self.snapshot()[0], (1,))

    async def test_stale_revision_from_an_unselected_legacy_record_cannot_overwrite(self):
        old = await self.save(self.local)
        current = await self.save(self.local, OTHER_GROUP, "Newer", at=NOW + timedelta(seconds=1))
        with self.assertRaises(TimetableConflictError):
            await self.save(self.shared, KEY, "Wrong", revision=old.revision)
        self.assertEqual(await self.shared.get_import(KEY), current)

    async def test_concurrent_first_imports_in_two_groups_have_exactly_one_winner(self):
        other = TimetableStore(self.path, group_isolation_enabled=False)
        self.addCleanup(other.close)
        results = await asyncio.gather(
            self.shared.save(KEY, "a.ics", "a" * 64, parsed(course("A"))),
            other.save(OTHER_GROUP, "b.ics", "b" * 64, parsed(course("B"))),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, StoredTimetable) for r in results), 1)
        self.assertEqual(sum(isinstance(r, TimetableConflictError) for r in results), 1)
        self.assertEqual(await self.shared.get_import(KEY), await other.get_import(OTHER_GROUP))

    async def test_concurrent_shared_updates_in_two_groups_have_exactly_one_winner(self):
        saved = await self.save(self.shared)
        other = TimetableStore(self.path, group_isolation_enabled=False)
        self.addCleanup(other.close)
        results = await asyncio.gather(
            self.shared.save(OTHER_GROUP, "a.ics", "a" * 64, parsed(course("A")), expected_revision=saved.revision),
            other.save(("300", "111"), "b.ics", "b" * 64, parsed(course("B")), expected_revision=saved.revision),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, StoredTimetable) for r in results), 1)
        self.assertEqual(sum(isinstance(r, TimetableConflictError) for r in results), 1)
        self.assertEqual(await self.shared.get_import(KEY), await other.get_import(OTHER_GROUP))

    async def test_failed_shared_update_rolls_back_the_original_version(self):
        saved = await self.save(self.shared)
        duplicate = course("Duplicate primary key")
        before = self.snapshot()
        with self.assertLogs("qq-bot", level="WARNING"), self.assertRaises(TimetableStoreError):
            await self.shared.save(OTHER_GROUP, "bad.ics", "b" * 64, parsed(duplicate, duplicate),
                                   expected_revision=saved.revision)
        self.assertEqual(before, self.snapshot())

    async def test_clock_rollback_does_not_change_the_selected_shared_record(self):
        await self.save(self.local, at=NOW)
        saved = await self.save(self.local, OTHER_GROUP, at=NOW + timedelta(seconds=1))
        updated = await self.save(self.shared, KEY, "Clock rollback update", at=NOW - timedelta(hours=1),
                                  revision=saved.revision)
        self.assertEqual(await self.shared.get_import(KEY), updated)
        self.assertGreaterEqual(updated.updated_at, saved.updated_at)


class SharedWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "timetable.sqlite3"
        self.plugin = TimetablePlugin(SETTINGS, database_path=self.path, images_enabled=False)
        self.addCleanup(self.plugin.close)
        self.bot = SimpleNamespace(
            _send_reply=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 123}}),
            _send_action_request=AsyncMock(return_value={"data": {"url": "https://example.invalid/test.ics"}}),
            _get_group_member_list=AsyncMock(return_value=[{"group_id": 200, "user_id": 111, "card": "Current group name"}]),
        )
        self.download = self.enterContext(patch("plugins.timetable.download_calendar_file",
                                               return_value=inspect_calendar_payload(CALENDAR)))
        self.parser = self.enterContext(patch("plugins.timetable.parse_calendar_isolated", new_callable=AsyncMock))
        self.parser.side_effect = parse_calendar
        self.enterContext(patch("plugins.timetable._now", return_value=NOW))

    async def command(self, command, *, group=100, user=111, mentions=()):
        event = {"message_type": "group", "group_id": group, "user_id": user,
                 "sender": {"user_id": user, "card": "My current group name"}}
        await self.plugin.handle(self.bot, None, event, CommandContext(command, list(mentions)))

    async def upload(self, *, group=100, user=111, content=CALENDAR):
        self.download.return_value = inspect_calendar_payload(content)
        await self.plugin.handle_notice(self.bot, None, {
            "post_type": "notice", "notice_type": "group_upload", "group_id": group, "user_id": user,
            "file": {"id": f"synthetic-{group}-{user}", "name": "synthetic.ics", "size": len(content)},
        })

    async def first_import(self):
        await self.command("/导入课表")
        await self.upload()
        await self.command("/已导入")
        self.assertIn("导入成功", self.latest())

    def latest(self):
        return text(self.bot._send_reply.await_args.args[2])

    async def test_unimported_account_gets_the_import_guide_in_shared_mode(self):
        await self.command("/更新课表", group=200)
        self.assertIn("尚未导入", self.latest())
        self.assertIn("/导入课表", self.latest())
        self.assertNotIn(("200", "111"), self.plugin.sessions)

    async def test_today_uses_same_qq_in_other_group_and_ignores_extra_mentions(self):
        await self.first_import()
        await self.plugin.store.save(("100", "222"), "other.ics", "b" * 64, parsed(course("Other member")))
        await self.command("/今日课程", group=200, mentions=("222",))
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Other member", self.latest())
        event, message = self.bot._send_reply.await_args.args[1:]
        self.assertEqual(event["group_id"], 200)
        self.assertEqual(message[0], {"type": "at", "data": {"qq": "111"}})
        await self.command("/今日课程", group=200, user=333)
        self.assertIn("尚未导入", self.latest())

    async def test_current_only_shows_present_members_using_requesting_group_names(self):
        await self.first_import()
        await self.plugin.store.save(("100", "333"), "outsider.ics", "c" * 64, parsed(course("Private outsider course")))
        await self.command("/课ing", group=200)
        self.assertIn("Current group name", self.latest())
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Private outsider course", self.latest())
        self.bot._get_group_member_list.assert_awaited_with(None, "200")

    async def test_other_group_reimport_is_rejected_and_confirmation_is_idempotent(self):
        await self.first_import()
        saved = await self.plugin.store.get_import(KEY)
        await self.command("/导入课表", group=200)
        self.assertIn("不能重复导入", self.latest())
        self.assertNotIn(("200", "111"), self.plugin.sessions)
        await self.command("/已导入", group=200)
        self.assertIn("无需重复确认", self.latest())
        self.assertEqual(await self.plugin.store.get_import(OTHER_GROUP), saved)
        self.assertEqual(self.parser.await_count, 1)

    async def test_shared_update_can_start_in_another_group_and_replaces_selected_source(self):
        await self.first_import()
        await self.command("/更新课表", group=200)
        self.assertIn("跨群共享", self.latest())
        await self.upload(group=200, content=CALENDAR.replace(b"Course A", b"Shared update"))
        await self.command("/已导入", group=200)
        self.assertIn("更新成功", self.latest())
        await self.command("/今日课程", group=100)
        self.assertIn("Shared update", self.latest())
        self.assertNotIn("Course A", self.latest())
        self.assertEqual((await self.plugin.store.get_import(OTHER_GROUP)).group_id, "100")

    async def test_upload_confirmation_and_cancellation_remain_group_bound(self):
        await self.command("/导入课表")
        self.assertIn("其他共同群成员", self.latest())
        await self.upload(group=200)
        self.download.assert_not_called()
        await self.command("/取消导入", group=200)
        self.assertIn(KEY, self.plugin.sessions)
        await self.upload()
        await self.command("/已导入", group=200)
        self.assertIn("没有有效", self.latest())
        self.parser.assert_not_awaited()
        self.assertIsNone(await self.plugin.store.get_import(OTHER_GROUP))
        await self.command("/已导入")
        self.assertIn("导入成功", self.latest())

    async def test_two_group_first_import_sessions_cannot_overwrite_each_other(self):
        await self.command("/导入课表")
        await self.command("/导入课表", group=200)
        await self.upload()
        await self.upload(group=200, content=CALENDAR.replace(b"Course A", b"Rejected course"))
        await self.command("/已导入")
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command("/已导入", group=200)
        self.assertIn("未覆盖", self.latest())
        self.assertNotIn(OTHER_GROUP, self.plugin.sessions)
        await self.command("/今日课程", group=200)
        self.assertIn("Course A", self.latest())
        self.assertNotIn("Rejected course", self.latest())

    async def test_stale_update_session_in_another_group_cannot_replace_new_version(self):
        await self.first_import()
        for group in (200, 300):
            await self.command("/更新课表", group=group)
            await self.upload(group=group, content=CALENDAR.replace(b"Course A", f"Update {group}".encode()))
        await self.command("/已导入", group=200)
        with self.assertLogs("qq-bot", level="WARNING"):
            await self.command("/已导入", group=300)
        self.assertIn("已发生变更", self.latest())
        await self.command("/今日课程", group=300)
        self.assertIn("Update 200", self.latest())
        self.assertNotIn("Update 300", self.latest())

    async def test_parse_failure_or_cancel_does_not_change_shared_courses(self):
        await self.first_import()
        saved = await self.plugin.store.get_import(KEY)
        await self.command("/更新课表", group=200)
        await self.upload(group=200)
        self.parser.side_effect = TimetableParseError("Synthetic parse failure")
        await self.command("/已导入", group=200)
        self.assertEqual(await self.plugin.store.get_import(OTHER_GROUP), saved)
        await self.command("/取消导入", group=200)
        self.assertEqual(await self.plugin.store.get_import(KEY), saved)

    async def test_member_lookup_failure_never_falls_back_to_global_database_members(self):
        await self.first_import()
        self.bot._get_group_member_list.side_effect = RuntimeError("Synthetic unavailable response")
        with patch.object(self.plugin.store, "current_courses", wraps=self.plugin.store.current_courses) as query:
            with self.assertLogs("qq-bot", level="WARNING"):
                await self.command("/课ing", group=200)
            query.assert_not_called()
        self.assertIn("获取群成员列表失败", self.latest())
        self.assertNotIn("Course A", self.latest())
        await self.command("/今日课程", group=200)
        self.assertIn("Course A", self.latest())

    async def test_timetable_help_reports_effective_shared_mode(self):
        await self.command("/课表", group=200)
        self.assertIn("群聊隔离：关闭", self.latest())
        self.assertIn("最近更新的一份", self.latest())


class SharedImageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "timetable.sqlite3"
        self.fetch_avatar = Mock(return_value=None)
        self.service = TimetableImageService(avatars=AvatarCache(fetch=self.fetch_avatar))
        self.service.plan = AsyncMock(wraps=self.service.plan)
        self.plugin = TimetablePlugin(SETTINGS, database_path=self.path, image_service=self.service)
        self.addCleanup(self.plugin.close)
        self.bot = SimpleNamespace(
            _send_reply=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 123}}),
            _get_group_member_list=AsyncMock(return_value=[{"group_id": 200, "user_id": 111, "card": "本群名片"}]),
        )
        self.enterContext(patch("plugins.timetable._now", return_value=NOW))

    async def command(self, command):
        await self.plugin.handle(self.bot, None,
            {"message_type": "group", "group_id": 200, "user_id": 111,
             "sender": {"user_id": 111, "card": "本群名片"}}, CommandContext(command, ["222"]))
        message = self.bot._send_reply.await_args.args[2]
        self.assertEqual(message[0], {"type": "at", "data": {"qq": "111"}})
        self.assertTrue(self.bot._send_reply.await_args.kwargs.get("wait_for_response"))
        image = next(item for item in message if item["type"] == "image")
        self.assertTrue(image["data"]["file"].startswith("base64://"))
        with Image.open(io.BytesIO(base64.b64decode(image["data"]["file"][9:]))) as png:
            self.assertEqual(png.format, "PNG")
            self.assertEqual(png.width, 1560)
        return self.service.plan.await_args.args[0]

    async def test_today_image_reuses_other_group_import_and_keeps_requester_ownership(self):
        await self.plugin.store.save(KEY, "a.ics", "a" * 64, parsed(course("共享课程")))
        await self.plugin.store.save(("100", "222"), "b.ics", "b" * 64, parsed(course("他人的课程")))
        view = await self.command("/今日课程")
        self.assertEqual([(m.user_id, m.name) for m in view.members], [("111", "本群名片")])
        self.assertEqual([c.name for c in view.members[0].courses], ["共享课程"])
        self.bot._get_group_member_list.assert_not_awaited()
        self.fetch_avatar.assert_called_once_with("111")

    async def test_current_image_uses_shared_updates_without_showing_outsiders(self):
        saved = await self.plugin.store.save(KEY, "a.ics", "a" * 64, parsed(course("原共享课程")))
        await self.plugin.store.save(("300", "333"), "c.ics", "c" * 64, parsed(course("非本群成员课程")))
        before = await self.command("/课ing")
        self.assertEqual([m.user_id for m in before.members], ["111"])
        self.assertEqual(before.members[0].courses[0].name, "原共享课程")
        await self.plugin.store.save(("400", "111"), "new.ics", "d" * 64, parsed(course("新共享课程")),
                                     expected_revision=saved.revision)
        after = await self.command("/课ing")
        self.assertEqual([(m.user_id, m.name) for m in after.members], [("111", "本群名片")])
        self.assertEqual(after.members[0].courses[0].name, "新共享课程")
        self.assertEqual(list(self.path.parent.glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()
