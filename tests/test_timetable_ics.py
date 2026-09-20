"""Synthetic calendars only: never commit the student's original ICS metadata."""

import asyncio
import calendar as python_calendar
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from plugins.timetable_ics import (
    FLOATING_TIME_WARNING,
    SHANGHAI,
    TimetableParseError,
    parse_calendar,
    parse_calendar_isolated,
)


def event(*, uid="test-course", name="Course A", start="DTSTART;TZID=Asia/Shanghai:20260907T095500",
          end="DTEND;TZID=Asia/Shanghai:20260907T112000", rule=None, extra=()):
    lines = ["BEGIN:VEVENT"]
    if uid is not None:
        lines.append("UID:" + uid)
    if name is not None:
        lines.append("SUMMARY:" + name)
    lines.extend(value for value in (start, end) if value is not None)
    if rule is not None:
        lines.append("RRULE:" + rule)
    lines.extend(extra)
    return "\r\n".join(lines + ["END:VEVENT"])


def calendar(*events, extra=()):
    return ("\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *extra, *events, "END:VCALENDAR", ""])).encode("utf-8")


class CalendarParserTest(unittest.TestCase):
    def parse(self, **kwargs):
        return parse_calendar(calendar(event(**kwargs)))

    def reject(self, payload, text=None):
        with self.assertRaises(TimetableParseError) as caught:
            parse_calendar(payload)
        if text is not None:
            self.assertIn(text, str(caught.exception))
        return str(caught.exception)

    def test_weekly_count_expands_and_keeps_timezone(self):
        result = self.parse(rule="FREQ=WEEKLY;COUNT=8;INTERVAL=1")
        self.assertEqual((result.source_event_count, result.series_count, len(result.occurrences)), (1, 1, 8))
        first, last = result.occurrences[0], result.occurrences[-1]
        self.assertEqual(first.starts_at, datetime(2026, 9, 7, 9, 55, tzinfo=SHANGHAI))
        self.assertEqual(last.starts_at, datetime(2026, 10, 26, 9, 55, tzinfo=SHANGHAI))
        self.assertEqual(first.ends_at - first.starts_at, timedelta(minutes=85))
        self.assertEqual(first.recurrence_id.utcoffset(), timedelta(0))
        self.assertEqual(first.starts_at.tzinfo.key, "Asia/Shanghai")
        self.assertEqual(result.warnings, ())

    def test_synthetic_sample_shape_is_15_events_and_172_occurrences(self):
        counts = [8] * 4 + [12] * 6 + [16] * 4 + [4]
        offsets = [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 4, 3]
        fixtures = []
        for index, (count, offset) in enumerate(zip(counts, offsets)):
            start = datetime(2026, 8, 31, 9, 55) + timedelta(days=offset)
            end = start + timedelta(minutes=85)
            fixtures.append(event(uid=f"synthetic-{index}", name=f"Course {index}",
                start=f"DTSTART;TZID=Asia/Shanghai:{start:%Y%m%dT%H%M%S}",
                end=f"DTEND;TZID=Asia/Shanghai:{end:%Y%m%dT%H%M%S}",
                rule=f"FREQ=WEEKLY;COUNT={count};INTERVAL=1"))
        result = parse_calendar(calendar(*fixtures))
        self.assertEqual(result.source_event_count, 15)
        self.assertEqual(len(result.occurrences), 172)
        self.assertEqual(str(result.occurrences[0].starts_at.date()), "2026-08-31")
        self.assertEqual(str(result.occurrences[-1].ends_at.date()), "2026-12-18")

    def test_single_nonrepeating_event(self):
        self.assertEqual(len(self.parse().occurrences), 1)

    def test_utc_time_converts_across_china_date_boundary(self):
        item = self.parse(start="DTSTART:20260906T230000Z", end="DTEND:20260907T003000Z").occurrences[0]
        self.assertEqual(item.starts_at, datetime(2026, 9, 7, 7, tzinfo=SHANGHAI))
        self.assertEqual(item.ends_at.hour, 8)
        self.assertEqual(item.ends_at.minute, 30)

    def test_utc_tzid_without_z_is_valid(self):
        result = self.parse(start="DTSTART;TZID=UTC:20260907T015500", end="DTEND;TZID=UTC:20260907T032000")
        self.assertEqual(result.occurrences[0].starts_at.hour, 9)
        self.assertEqual(result.occurrences[0].starts_at.minute, 55)

    def test_tzid_with_utc_mark_is_rejected_before_library_normalizes_it(self):
        self.reject(calendar(event(start="DTSTART;TZID=Asia/Shanghai:20260907T095500Z")), "同时指定")
        self.reject(calendar(event(extra=("EXDATE;TZID=Asia/Shanghai:20260907T015500Z",))), "同时指定")

    def test_floating_time_uses_china_and_warns_once(self):
        result = parse_calendar(calendar(
            event(start="DTSTART:20260907T095500", end="DTEND:20260907T112000"),
            event(uid="second", start="DTSTART:20260908T095500", end="DTEND:20260908T112000"),
        ))
        self.assertEqual(result.warnings, (FLOATING_TIME_WARNING,))
        self.assertTrue(all(item.starts_at.hour == 9 for item in result.occurrences))

    def test_folded_unicode_and_escaped_text(self):
        result = parse_calendar(b"\xef\xbb\xbf" + calendar(event(
            name="离散数学\\,基础\\n第二\r\n 行", extra=("LOCATION:教三\\,3502",),
        )))
        self.assertEqual(result.occurrences[0].name, "离散数学,基础\n第二行")
        self.assertEqual(result.occurrences[0].location, "教三,3502")

    def test_iana_zone_recurrence_keeps_local_time_across_dst(self):
        result = self.parse(start="DTSTART;TZID=America/New_York:20261026T090000",
                            end="DTEND;TZID=America/New_York:20261026T100000",
                            rule="FREQ=WEEKLY;COUNT=3")
        self.assertEqual([item.starts_at.hour for item in result.occurrences], [21, 22, 22])
        self.assertTrue(all(item.ends_at - item.starts_at == timedelta(hours=1) for item in result.occurrences))

    def test_unknown_timezone_does_not_silently_become_floating(self):
        self.reject(calendar(event(start="DTSTART;TZID=Invalid/SecretZone:20260907T095500")), "时区")

    def test_nonexistent_dst_start_is_rejected(self):
        self.reject(calendar(event(start="DTSTART;TZID=America/New_York:20260308T023000",
                                   end="DTEND;TZID=America/New_York:20260308T043000")), "夏令时")

    def test_nonexistent_dst_recurrence_does_not_count(self):
        result = self.parse(start="DTSTART;TZID=America/New_York:20260301T023000",
                            end="DTEND;TZID=America/New_York:20260301T033000",
                            rule="FREQ=WEEKLY;COUNT=2")
        self.assertEqual([item.starts_at.day for item in result.occurrences], [1, 15])

    def test_duration_and_overnight_lesson(self):
        result = self.parse(start="DTSTART;TZID=Asia/Shanghai:20260907T233000", end=None,
                            extra=("DURATION:PT1H30M",))
        item = result.occurrences[0]
        self.assertEqual(item.ends_at, datetime(2026, 9, 8, 1, tzinfo=SHANGHAI))

    def test_invalid_or_missing_duration_is_rejected(self):
        for duration in (None, "PT0S", "-PT1H", "P1D", "P2D"):
            with self.subTest(duration=duration):
                self.reject(calendar(event(end=None, extra=("DURATION:" + duration,) if duration else ())))
        self.reject(calendar(event(end="DTEND;TZID=Asia/Shanghai:20260907T090000")), "晚于")
        self.reject(calendar(event(extra=("DURATION:PT1H",))), "同时")

    def test_all_day_events_are_not_silently_dropped(self):
        self.reject(calendar(event(start="DTSTART;VALUE=DATE:20260907", end="DTEND;VALUE=DATE:20260908")), "全天")

    def test_until_includes_the_last_matching_start(self):
        result = self.parse(rule="FREQ=WEEKLY;UNTIL=20260921T015500Z")
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 14, 21])

    def test_floating_until_is_supported(self):
        result = self.parse(start="DTSTART:20260907T095500", end="DTEND:20260907T112000",
                            rule="FREQ=WEEKLY;UNTIL=20260921T095500")
        self.assertEqual(len(result.occurrences), 3)
        self.assertEqual(result.warnings, (FLOATING_TIME_WARNING,))

    def test_until_type_timezone_and_order_are_validated(self):
        for until in ("20260921", "20260921T095500", "20260801T000000Z"):
            with self.subTest(until=until):
                self.reject(calendar(event(rule="FREQ=WEEKLY;UNTIL=" + until)), "UNTIL")
        self.reject(calendar(event(start="DTSTART:20260907T095500", end="DTEND:20260907T112000",
                                   rule="FREQ=WEEKLY;UNTIL=20260921T015500Z")), "UNTIL")

    def test_count_and_until_are_required_and_mutually_exclusive(self):
        for rule in ("FREQ=WEEKLY", "FREQ=WEEKLY;COUNT=2;UNTIL=20260921T015500Z"):
            with self.subTest(rule=rule):
                self.reject(calendar(event(rule=rule)), "COUNT 或 UNTIL")

    def test_biweekly_multiple_weekdays(self):
        result = self.parse(rule="FREQ=WEEKLY;COUNT=4;INTERVAL=2;BYDAY=MO,WE")
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 9, 21, 23])

    def test_week_start_is_explicit_and_not_host_global_state(self):
        kwargs = dict(start="DTSTART;TZID=Asia/Shanghai:20260906T095500",
                      end="DTEND;TZID=Asia/Shanghai:20260906T112000")
        rule = "FREQ=WEEKLY;INTERVAL=2;BYDAY=SU,MO;COUNT=4"
        old = python_calendar.firstweekday()
        try:
            python_calendar.setfirstweekday(python_calendar.SUNDAY)
            default = self.parse(**kwargs, rule=rule)
            sunday = self.parse(**kwargs, rule=rule + ";WKST=SU")
        finally:
            python_calendar.setfirstweekday(old)
        self.assertEqual([item.starts_at.day for item in default.occurrences], [6, 14, 20, 28])
        self.assertEqual([item.starts_at.day for item in sunday.occurrences], [6, 7, 20, 21])

    def test_monthly_ordinal_weekday(self):
        result = self.parse(rule="FREQ=MONTHLY;COUNT=3;BYDAY=1MO")
        self.assertEqual([(item.starts_at.month, item.starts_at.day) for item in result.occurrences], [(9, 7), (10, 5), (11, 2)])

    def test_monthly_monthday_and_yearly_rules(self):
        result = self.parse(rule="FREQ=MONTHLY;COUNT=3;BYMONTHDAY=7")
        self.assertEqual([item.starts_at.month for item in result.occurrences], [9, 10, 11])
        yearly = self.parse(rule="FREQ=YEARLY;COUNT=2;BYMONTH=9;BYMONTHDAY=7")
        self.assertEqual([item.starts_at.year for item in yearly.occurrences], [2026, 2027])

    def test_last_weekday_bysetpos(self):
        result = self.parse(start="DTSTART;TZID=Asia/Shanghai:20260831T095500",
                            end="DTEND;TZID=Asia/Shanghai:20260831T112000",
                            rule="FREQ=MONTHLY;COUNT=3;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1")
        self.assertEqual([(item.starts_at.month, item.starts_at.day) for item in result.occurrences], [(8, 31), (9, 30), (10, 30)])

    def test_rule_must_match_first_lesson(self):
        self.reject(calendar(event(rule="FREQ=WEEKLY;COUNT=3;BYDAY=TU")), "首次")

    def test_invalid_and_unsupported_rule_parts_are_rejected(self):
        rules = (
            "FREQ=SECONDLY;COUNT=3", "FREQ=MINUTELY;COUNT=3", "FREQ=HOURLY;COUNT=3",
            "FREQ=INVALID;COUNT=3", "FREQ=WEEKLY;COUNT=0", "FREQ=WEEKLY;COUNT=-1",
            "FREQ=WEEKLY;COUNT=3;INTERVAL=0", "FREQ=WEEKLY;COUNT=3;INTERVAL=999",
            "FREQ=WEEKLY;COUNT=3;BYHOUR=9", "FREQ=WEEKLY;COUNT=3;BYMINUTE=55",
            "FREQ=WEEKLY;COUNT=3;BYSECOND=0", "FREQ=YEARLY;COUNT=3;BYWEEKNO=1",
            "FREQ=WEEKLY;COUNT=3;WKST=XX", "FREQ=WEEKLY;COUNT=3;BYDAY=1MO",
            "FREQ=MONTHLY;COUNT=3;BYDAY=0MO", "FREQ=MONTHLY;COUNT=3;BYMONTHDAY=0",
            "FREQ=WEEKLY;COUNT=3;BYMONTHDAY=7", "FREQ=WEEKLY;COUNT=3;BYMONTH=13",
            "FREQ=MONTHLY;COUNT=3;BYSETPOS=1",
        )
        for rule in rules:
            with self.subTest(rule=rule):
                self.reject(calendar(event(rule=rule)))

    def test_rdate_union_deduplicates_equal_instants(self):
        result = self.parse(rule="FREQ=WEEKLY;COUNT=2", extra=(
            "RDATE;TZID=Asia/Shanghai:20260907T095500,20260908T095500",
            "RDATE:20260908T015500Z",
        ))
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 8, 14])

    def test_exdate_multiple_lines_and_zones(self):
        result = self.parse(rule="FREQ=WEEKLY;COUNT=4", extra=(
            "EXDATE;TZID=Asia/Shanghai:20260914T095500", "EXDATE:20260921T015500Z",
        ))
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 28])

    def test_exdate_can_remove_dtstart_and_no_rule(self):
        result = self.parse(extra=("RDATE:20260908T015500Z", "EXDATE:20260907T015500Z"))
        self.assertEqual([item.starts_at.day for item in result.occurrences], [8])

    def test_period_and_date_only_rdate_are_rejected(self):
        for extra in ("RDATE;VALUE=PERIOD:20260908T015500Z/PT1H", "RDATE;VALUE=DATE:20260908"):
            with self.subTest(extra=extra):
                self.reject(calendar(event(extra=(extra,))), "全天")

    def test_override_inherits_name_location_and_duration(self):
        master = event(rule="FREQ=WEEKLY;COUNT=3", extra=("LOCATION:Room A",))
        change = event(name=None, start="DTSTART;TZID=Asia/Shanghai:20260915T140000", end=None,
                       extra=("RECURRENCE-ID:20260914T015500Z",))
        result = parse_calendar(calendar(change, master))  # Overrides may precede the master.
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 15, 21])
        moved = result.occurrences[1]
        self.assertEqual((moved.name, moved.location, moved.ends_at.hour, moved.ends_at.minute), ("Course A", "Room A", 15, 25))
        self.assertEqual(moved.recurrence_id, datetime(2026, 9, 14, 1, 55, tzinfo=timezone.utc))

    def test_override_can_replace_name_location_and_end(self):
        result = parse_calendar(calendar(
            event(rule="FREQ=WEEKLY;COUNT=2"),
            event(name="Replacement", start="DTSTART:20260915T020000Z", end="DTEND:20260915T030000Z",
                  extra=("RECURRENCE-ID;TZID=Asia/Shanghai:20260914T095500", "LOCATION:Room B")),
        ))
        self.assertEqual(result.occurrences[1].name, "Replacement")
        self.assertEqual(result.occurrences[1].location, "Room B")
        self.assertEqual(result.source_event_count, 2)
        self.assertEqual(result.series_count, 1)

    def test_cancelled_override_without_times_removes_one_instance(self):
        result = parse_calendar(calendar(
            event(rule="FREQ=WEEKLY;COUNT=3"),
            event(name=None, start=None, end=None, extra=("RECURRENCE-ID:20260914T015500Z", "STATUS:CANCELLED")),
        ))
        self.assertEqual([item.starts_at.day for item in result.occurrences], [7, 21])

    def test_cancelled_master_is_excluded(self):
        result = parse_calendar(calendar(event(extra=("STATUS:CANCELLED",)), event(uid="active")))
        self.assertEqual([item.uid for item in result.occurrences], ["active"])

    def test_empty_result_is_not_a_successful_timetable(self):
        self.reject(calendar(event(extra=("STATUS:CANCELLED",))), "没有可用课程")
        self.reject(calendar(event(extra=("EXDATE:20260907T015500Z",))), "没有可用课程")

    def test_duplicate_masters_and_overrides_are_ambiguous(self):
        self.reject(calendar(event(), event()), "重复 UID")
        change = event(extra=("RECURRENCE-ID:20260907T015500Z",))
        self.reject(calendar(event(), change, change), "多个改期")

    def test_orphan_and_out_of_set_overrides_are_rejected(self):
        change = event(extra=("RECURRENCE-ID:20260914T015500Z",))
        self.reject(calendar(change), "缺少对应 UID")
        self.reject(calendar(event(), change), "未匹配")

    def test_range_and_recurring_overrides_are_rejected(self):
        self.reject(calendar(event(), event(extra=("RECURRENCE-ID;RANGE=THISANDFUTURE:20260907T015500Z",))), "RANGE")
        self.reject(calendar(event(), event(rule="FREQ=WEEKLY;COUNT=2",
                    extra=("RECURRENCE-ID:20260907T015500Z",))), "单次改期")

    def test_conflicting_exclusion_and_override_is_rejected(self):
        self.reject(calendar(event(extra=("EXDATE:20260907T015500Z",)),
                    event(extra=("RECURRENCE-ID:20260907T015500Z",))), "冲突")

    def test_equal_times_with_different_uids_are_not_lost(self):
        result = parse_calendar(calendar(event(uid="b"), event(uid="a")))
        self.assertEqual([item.uid for item in result.occurrences], ["a", "b"])

    def test_missing_required_fields_are_not_filled_with_dummy_values(self):
        for kwargs in ({"uid": None}, {"name": None}, {"start": None}, {"name": ""}):
            with self.subTest(kwargs=kwargs):
                self.reject(calendar(event(**kwargs)))

    def test_duplicate_single_value_properties_are_rejected(self):
        for extra in ("SUMMARY:Other", "DTSTART:20260907T015500Z", "DTEND:20260907T032000Z"):
            with self.subTest(extra=extra):
                self.reject(calendar(event(extra=(extra,))), "重复")
        self.reject(calendar(event(rule="FREQ=WEEKLY;COUNT=2", extra=("RRULE:FREQ=WEEKLY;COUNT=3",))), "重复")

    def test_invalid_calendar_structure_and_dates_are_rejected(self):
        payloads = (
            b"not an ICS file", calendar(),
            calendar(event()).replace(b"VERSION:2.0", b"VERSION:1.0"),
            calendar(event()).replace(b"END:VEVENT", b"END:VTODO"),
            calendar(event()).replace(b"20260907T095500", b"20260230T095500"),
            calendar(event()).replace(b"SUMMARY:Course A", b"BROKEN LINE"),
            calendar(event()) + calendar(event(uid="another-calendar")),
            calendar(event()).replace(b"BEGIN:VEVENT", b"BEGIN:VTODO").replace(b"END:VEVENT", b"END:VTODO"),
        )
        for payload in payloads:
            with self.subTest(payload=payload[:50]):
                self.reject(payload)

    def test_unsupported_calendar_methods_exrule_and_status(self):
        self.reject(calendar(event(), extra=("METHOD:CANCEL",)), "完整课表")
        self.reject(calendar(event(extra=("EXRULE:FREQ=WEEKLY;COUNT=1",))), "EXRULE")
        self.reject(calendar(event(extra=("STATUS:INVALID",))), "STATUS")

    def test_metadata_links_and_alarms_are_not_followed_or_retained(self):
        metadata = "private-student-metadata"
        payload = calendar(event(extra=(
            "DESCRIPTION:" + metadata, "ATTACH:https://example.invalid/secret",
            "URL:https://example.invalid/calendar", "X-APPLE-STRUCTURED-LOCATION:geo:0,0",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "DESCRIPTION:" + metadata, "END:VALARM",
        )), extra=("X-WR-CALNAME:" + metadata,))
        with patch("urllib.request.urlopen", side_effect=AssertionError("Must not fetch calendar links")):
            result = parse_calendar(payload)
        self.assertNotIn(metadata, repr(result))
        self.assertNotIn("https://", repr(result))

    def test_iana_vtimezone_is_accepted_but_cannot_hide_events(self):
        zone = "\r\n".join(("BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD",
                            "DTSTART:19700101T000000", "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800",
                            "END:STANDARD", "END:VTIMEZONE"))
        result = parse_calendar(calendar(zone, event()))
        self.assertEqual(result.occurrences[0].starts_at.hour, 9)
        hidden = zone.replace("END:VTIMEZONE", event(uid="hidden") + "\r\nEND:VTIMEZONE")
        self.reject(calendar(hidden, event()), "嵌套")

    def test_library_errors_do_not_leak_calendar_data(self):
        with patch("plugins.timetable_ics.Calendar.from_ical", side_effect=ValueError("private-student-and-token")):
            message = self.reject(calendar(event()))
        self.assertNotIn("private-student", message)

    def test_byte_and_event_limits(self):
        self.reject(b" " * (10 * 1024 * 1024 + 1), "上限")
        with patch("plugins.timetable_ics.MAX_SOURCE_EVENTS", 1):
            self.reject(calendar(event(), event(uid="second")), "事件数量")

    def test_per_rule_and_whole_calendar_occurrence_caps(self):
        with patch("plugins.timetable_ics.MAX_OCCURRENCES", 3):
            self.reject(calendar(event(rule="FREQ=WEEKLY;COUNT=4")), "COUNT")
            self.reject(calendar(event(rule="FREQ=WEEKLY;COUNT=2"),
                                 event(uid="second", rule="FREQ=WEEKLY;COUNT=2")), "整份课表")

    def test_repeat_search_and_all_explicit_dates_have_a_horizon(self):
        self.reject(calendar(event(rule="FREQ=WEEKLY;COUNT=200")), "730")
        self.reject(calendar(event(rule="FREQ=WEEKLY;UNTIL=20991231T000000Z")), "730")
        self.reject(calendar(event(extra=("RDATE:20991231T000000Z",))), "730")
        self.reject(calendar(event(rule="FREQ=YEARLY;COUNT=2;BYMONTH=2;BYMONTHDAY=30")), "730")

    def test_long_content_lines_and_deep_components_are_rejected(self):
        self.reject(calendar(event(extra=("DESCRIPTION:" + "x" * 32_768,))), "字段过长")
        self.reject(calendar(event(extra=("BEGIN:A", "BEGIN:B", "BEGIN:C", "END:C", "END:B", "END:A"))), "嵌套")


class CalendarWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_matches_parser(self):
        payload = calendar(event(rule="FREQ=WEEKLY;COUNT=8", name="合成课程"))
        self.assertEqual(await parse_calendar_isolated(payload), parse_calendar(payload))

    async def test_real_worker_returns_safe_validation_error(self):
        with self.assertRaisesRegex(TimetableParseError, "无限重复"):
            await parse_calendar_isolated(calendar(event(rule="FREQ=WEEKLY")))

    async def test_real_worker_timeout_kills_and_reaps_process(self):
        create = asyncio.create_subprocess_exec
        created = []

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            created.append(process)
            return process

        with patch("plugins.timetable_ics.asyncio.create_subprocess_exec", side_effect=capture):
            with self.assertRaisesRegex(TimetableParseError, "超时"):
                await parse_calendar_isolated(calendar(event()), timeout_seconds=0.000001)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    async def test_real_worker_cancellation_kills_and_reaps_process(self):
        create = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        created = []

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            created.append(process)
            spawned.set()
            return process

        with patch("plugins.timetable_ics.asyncio.create_subprocess_exec", side_effect=capture):
            task = asyncio.create_task(parse_calendar_isolated(calendar(event())))
            await asyncio.wait_for(spawned.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(created[0].returncode)

    async def test_bad_worker_result_is_not_exposed(self):
        for returncode, stdout in ((1, b"private-student"), (0, b"invalid-private-json"), (0, b"{}")):
            with self.subTest(returncode=returncode, stdout=stdout):
                process = Mock(returncode=returncode, communicate=AsyncMock(return_value=(stdout, b"")))
                with patch("plugins.timetable_ics.asyncio.create_subprocess_exec", return_value=process):
                    with self.assertRaises(TimetableParseError) as caught:
                        await parse_calendar_isolated(calendar(event()))
                self.assertNotIn("private", str(caught.exception))

    async def test_communication_error_kills_and_reaps_worker(self):
        process = Mock(returncode=None, communicate=AsyncMock(side_effect=[OSError("private-token"), (b"", b"")]))
        with patch("plugins.timetable_ics.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaisesRegex(TimetableParseError, "通信中断") as caught:
                await parse_calendar_isolated(calendar(event()))
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)
        self.assertNotIn("private-token", str(caught.exception))

    async def test_oversized_input_never_spawns_worker(self):
        with patch("plugins.timetable_ics.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(TimetableParseError):
                await parse_calendar_isolated(b"x" * (10 * 1024 * 1024 + 1))
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
