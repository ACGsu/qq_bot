import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.timetable_ics import CourseOccurrence, ParsedTimetable, SHANGHAI
from plugins.timetable_store import TimetableConflictError, TimetableStore, TimetableStoreError


KEY = ("100", "111")


def when(day=7, hour=10, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=SHANGHAI)


def lesson(uid="course-a", *, start=None, end=None, name="Course A", recurrence_id=None):
    start = start or when()
    end = end or start + timedelta(hours=1)
    return CourseOccurrence(uid, recurrence_id or start.astimezone(timezone.utc), name, start, end, "Room 1")


def calendar(*courses):
    courses = courses or (lesson(),)
    return ParsedTimetable(len(courses), len({c.uid for c in courses}), tuple(courses))


class TimetableStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "nested" / "timetable.sqlite3"
        self.store = TimetableStore(self.path, timeout=0.1)

    async def asyncTearDown(self):
        self.store.close()
        self.assertEqual(self.store._pending, 0)

    async def save(self, parsed=None, key=KEY, revision=None):
        return await self.store.save(key, "test.ics", "a" * 64, parsed or calendar(), expected_revision=revision)

    async def test_database_is_lazy_and_schema_has_no_raw_calendar_columns(self):
        self.assertFalse(self.path.exists())
        self.assertIsNone(await self.store.get_import(KEY))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            names = {row[1] for row in connection.execute("PRAGMA table_info(timetable_occurrences)")}
            self.assertEqual(names, {"group_id", "user_id", "uid", "recurrence_id", "name", "starts_at", "ends_at", "location"})
            names = {row[1] for row in connection.execute("PRAGMA table_info(timetable_imports)")}
            self.assertNotIn("content", names)
            self.assertNotIn("description", names)

    async def test_round_trip_preserves_original_recurrence_identity(self):
        moved = lesson(start=when(hour=12), recurrence_id=when(hour=10).astimezone(timezone.utc))
        saved = await self.save(calendar(moved))
        self.assertEqual(saved, await self.store.get_import(KEY))
        self.assertEqual(saved.occurrence_count, 1)
        self.assertEqual(saved.sha256, "a" * 64)
        self.assertEqual(await self.store.today_courses(KEY, when()), (moved,))
        actual = (await self.store.current_courses(KEY[0], when(hour=12)))[0].courses[0]
        self.assertEqual(actual, moved)
        self.assertEqual(actual.recurrence_id.utcoffset(), timedelta(0))
        self.assertEqual(actual.starts_at.utcoffset(), timedelta(hours=8))

    async def test_reopening_database_preserves_import_and_courses(self):
        saved = await self.save()
        self.store.close()
        self.store = TimetableStore(self.path)
        self.assertEqual(await self.store.get_import(KEY), saved)
        self.assertEqual(await self.store.today_courses(KEY, when()), (lesson(),))

    async def test_group_and_member_isolation(self):
        await self.save()
        await self.save(calendar(lesson(name="Member B")), key=("100", "222"))
        await self.save(calendar(lesson(name="Other group")), key=("200", "111"))
        self.assertEqual([c.name for c in await self.store.today_courses(KEY, when())], ["Course A"])
        members = await self.store.current_courses("100", when())
        self.assertEqual([m.user_id for m in members], ["111", "222"])
        self.assertEqual([c.name for m in members for c in m.courses], ["Course A", "Member B"])
        self.assertEqual(await self.store.current_courses("300", when()), ())

    async def test_duplicate_first_import_never_overwrites(self):
        original = await self.save()
        with self.assertRaises(TimetableConflictError):
            await self.save(calendar(lesson(name="Wrong replacement")))
        self.assertEqual(await self.store.get_import(KEY), original)
        self.assertEqual(await self.store.today_courses(KEY, when()), (lesson(),))

    async def test_update_requires_an_existing_import(self):
        with self.assertRaises(TimetableConflictError):
            await self.save(revision="missing")
        self.assertIsNone(await self.store.get_import(KEY))

    async def test_update_replaces_all_courses_without_duplicates(self):
        original = await self.save(calendar(lesson(), lesson("course-b", start=when(hour=14))))
        replacement = calendar(lesson("course-c", name="New course"))
        updated = await self.save(replacement, revision=original.revision)
        self.assertNotEqual(updated.revision, original.revision)
        self.assertEqual(updated.imported_at, original.imported_at)
        self.assertEqual(updated.occurrence_count, 1)
        self.assertEqual(await self.store.today_courses(KEY, when()), replacement.occurrences)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM timetable_imports").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM timetable_occurrences").fetchone()[0], 1)

    async def test_failed_update_rolls_back_metadata_and_deleted_old_courses(self):
        original = await self.save()
        duplicate = lesson("new", name="Must roll back")
        with self.assertLogs("qq-bot", level="WARNING"):
            with self.assertRaises(TimetableStoreError):
                await self.save(calendar(duplicate, duplicate), revision=original.revision)
        self.assertEqual(await self.store.get_import(KEY), original)
        self.assertEqual(await self.store.today_courses(KEY, when()), (lesson(),))

    async def test_failed_first_import_leaves_no_registration_or_partial_courses(self):
        with self.assertLogs("qq-bot", level="WARNING"):
            with self.assertRaises(TimetableStoreError):
                await self.save(calendar(lesson(), lesson()))
        self.assertIsNone(await self.store.get_import(KEY))
        self.assertEqual(await self.store.current_courses("100", when()), ())
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM timetable_occurrences").fetchone()[0], 0)

    async def test_stale_revision_cannot_replace_newer_import(self):
        first = await self.save()
        second = await self.save(calendar(lesson(name="Second")), revision=first.revision)
        with self.assertRaises(TimetableConflictError):
            await self.save(calendar(lesson(name="Stale")), revision=first.revision)
        self.assertEqual(await self.store.get_import(KEY), second)
        self.assertEqual((await self.store.today_courses(KEY, when()))[0].name, "Second")

    async def test_two_stores_racing_to_update_have_exactly_one_winner(self):
        original = await self.save()
        other = TimetableStore(self.path)
        try:
            results = await asyncio.gather(
                self.save(calendar(lesson(name="First")), revision=original.revision),
                other.save(KEY, "other.ics", "b" * 64, calendar(lesson(name="Other")), expected_revision=original.revision),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(item, TimetableConflictError) for item in results), 1)
            self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
            self.assertIn((await self.store.today_courses(KEY, when()))[0].name, ("First", "Other"))
        finally:
            other.close()

    async def test_current_interval_is_start_inclusive_and_end_exclusive(self):
        await self.save()
        for moment, count in ((when(hour=9, minute=59), 0), (when(), 1), (when(hour=10, minute=59), 1), (when(hour=11), 0)):
            with self.subTest(moment=moment):
                members = await self.store.current_courses("100", moment)
                self.assertEqual(len(members), 1)
                self.assertEqual(len(members[0].courses), count)

    async def test_overlapping_courses_and_idle_members_are_all_returned(self):
        await self.save(calendar(lesson(), lesson("second", name="Second at same time")))
        await self.save(calendar(lesson(start=when(hour=14))), key=("100", "222"))
        members = await self.store.current_courses("100", when())
        self.assertEqual([m.user_id for m in members], ["111", "222"])
        self.assertEqual(len(members[0].courses), 2)
        self.assertEqual(members[1].courses, ())

    async def test_current_member_filter_preserves_idle_members_and_group_isolation(self):
        await self.save()
        idle = await self.save(calendar(lesson(start=when(hour=14))), key=("100", "222"))
        await self.save(key=("200", "222"))
        members = await self.store.current_courses("100", when(), user_ids=("222", "333"))
        self.assertEqual([(m.user_id, m.courses) for m in members], [("222", ())])
        self.assertEqual(await self.store.get_import(("100", "222")), idle)
        self.assertIsNotNone(await self.store.get_import(KEY))

    async def test_current_member_filter_applies_before_result_limit(self):
        await self.save(calendar(lesson(), lesson("second")))
        await self.save(key=("100", "222"))
        with patch("plugins.timetable_store.MAX_QUERY_ROWS", 1):
            members = await self.store.current_courses("100", when(), user_ids=("222",))
            self.assertEqual([m.user_id for m in members], ["222"])
            with self.assertRaisesRegex(TimetableStoreError, "超过单次查询上限"):
                await self.store.current_courses("100", when(), user_ids=("111",))
        self.assertEqual(len((await self.store.current_courses("100", when()))[0].courses), 2)

    async def test_empty_member_filter_never_falls_back_to_all_imports(self):
        self.assertEqual(await self.store.current_courses("100", when(), user_ids=()), ())
        self.assertFalse(self.path.exists())
        await self.save()
        self.assertEqual(await self.store.current_courses("100", when(), user_ids=()), ())
        self.assertIsNotNone(await self.store.get_import(KEY))

    async def test_large_member_allowlist_is_parameterized_and_deduplicated(self):
        await self.save()
        users = [str(user) for user in range(1, 1501)] + ["111", "'); DROP TABLE timetable_imports; --"]
        members = await self.store.current_courses("100", when(), user_ids=users)
        self.assertEqual([m.user_id for m in members], ["111"])
        self.assertEqual(len(members[0].courses), 1)
        self.assertIsNotNone(await self.store.get_import(KEY))

    async def test_member_allowlist_does_not_change_persistent_schema_or_metadata(self):
        original = await self.save()
        with closing(sqlite3.connect(self.path)) as connection:
            before = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        await self.store.current_courses("100", when(), user_ids=("111",))
        with closing(sqlite3.connect(self.path)) as connection:
            after = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(after, before)
        self.assertEqual(await self.store.get_import(KEY), original)

    async def test_sqlite_online_backup_is_a_consistent_independent_snapshot(self):
        original = await self.save()
        backup_path = self.path.with_name("snapshot.sqlite3")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
                self.assertEqual(destination.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        await self.save(calendar(lesson(name="Updated after backup")), revision=original.revision)
        backup = TimetableStore(backup_path)
        try:
            self.assertEqual(await backup.get_import(KEY), original)
            self.assertEqual(await backup.today_courses(KEY, when()), (lesson(),))
        finally:
            backup.close()

    async def test_today_uses_shanghai_midnight_and_includes_cross_midnight_overlap(self):
        previous = lesson("previous", start=when(6, 23, 30), end=when(7, 0, 30))
        early = lesson("early", start=when(7, 0, 0))
        late = lesson("late", start=when(7, 23, 30), end=when(8, 0, 30))
        excluded_end = lesson("ended", start=when(6, 23), end=when(7, 0))
        excluded_start = lesson("tomorrow", start=when(8, 0))
        await self.save(calendar(late, excluded_start, early, excluded_end, previous))
        # UTC date is the previous day; Beijing is already September 7.
        utc_now = datetime(2026, 9, 6, 16, 15, tzinfo=timezone.utc)
        actual = await self.store.today_courses(KEY, utc_now)
        self.assertEqual(actual, (previous, early, late))
        current = (await self.store.current_courses("100", utc_now))[0].courses
        self.assertEqual(current, (previous, early))

    async def test_missing_import_and_no_courses_today_are_distinct(self):
        self.assertIsNone(await self.store.today_courses(KEY, when()))
        await self.save()
        self.assertEqual(await self.store.today_courses(KEY, when(day=8)), ())

    async def test_query_row_limit_refuses_instead_of_silently_truncating(self):
        await self.save(calendar(lesson(), lesson("second")))
        with patch("plugins.timetable_store.MAX_QUERY_ROWS", 1):
            for request in (self.store.current_courses("100", when()), self.store.today_courses(KEY, when())):
                with self.assertRaisesRegex(TimetableStoreError, "超过单次查询上限"):
                    await request

    async def test_sql_looking_calendar_text_is_stored_as_literal_data(self):
        name = "'); DROP TABLE timetable_imports; -- [CQ:at,qq=all]"
        course = lesson(uid=name, name=name)
        await self.save(calendar(course))
        self.assertEqual(await self.store.today_courses(KEY, when()), (course,))
        self.assertIsNotNone(await self.store.get_import(KEY))

    async def test_unsupported_schema_version_is_not_changed(self):
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(TimetableStoreError, "版本不兼容"):
            await self.store.get_import(KEY)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0], 0)

    async def test_invalid_datetime_never_creates_partial_import(self):
        course = replace(lesson(), starts_at=datetime(2026, 9, 7, 10))
        with self.assertLogs("qq-bot", level="WARNING"):
            with self.assertRaises(TimetableStoreError):
                await self.save(calendar(course))
        self.assertIsNone(await self.store.get_import(KEY))

    async def test_database_lock_failure_is_safe_and_does_not_block_event_loop(self):
        original = await self.save()
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertLogs("qq-bot", level="WARNING"):
                task = asyncio.create_task(self.save(revision=original.revision))
                await asyncio.wait_for(asyncio.sleep(0.01), 0.05)
                self.assertFalse(task.done())
                with self.assertRaisesRegex(TimetableStoreError, "暂时不可用"):
                    await task
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(await self.store.get_import(KEY), original)

    async def test_errors_hide_database_paths_and_arbitrary_exception_text(self):
        with patch.object(self.store, "_get_import", side_effect=OSError("private-path-and-token")):
            with self.assertLogs("qq-bot", level="WARNING") as logs:
                with self.assertRaises(TimetableStoreError) as error:
                    await self.store.get_import(KEY)
        self.assertNotIn("private-path-and-token", str(error.exception) + "".join(logs.output))

    async def test_cancelled_query_keeps_queue_slot_until_worker_really_finishes(self):
        started, release = threading.Event(), threading.Event()
        original = self.store._get_import

        def blocked(key):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return original(key)

        with patch.object(self.store, "_get_import", side_effect=blocked), patch("plugins.timetable_store.MAX_PENDING_DB_JOBS", 1):
            task = asyncio.create_task(self.store.get_import(KEY))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.store._pending, 1)
                with self.assertRaisesRegex(TimetableStoreError, "繁忙"):
                    await self.store.get_import(KEY)
            finally:
                release.set()
                async with asyncio.timeout(3):
                    while self.store._pending:
                        await asyncio.sleep(0.01)
        self.assertIsNone(await self.store.get_import(KEY))

    async def test_close_rejects_new_work_without_creating_a_database(self):
        self.store.close()
        with self.assertRaisesRegex(TimetableStoreError, "已关闭"):
            await self.store.get_import(KEY)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
