"""Synthetic fixtures only; renderer tests never use private ICS/avatar files."""
import io
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from plugins.timetable_ics import CourseOccurrence, SHANGHAI
from plugins.timetable_render import (
    MemberView, TimetableView, RenderLimits, TimetableRenderError,
    build_plan, render_page, course_state, course_progress, course_time, clean_text,
)

NOW = datetime(2026, 9, 8, 10, 20, tzinfo=SHANGHAI)


def course(name="测试课程", start=None, end=None, location="教三-3401"):
    start = start or NOW.replace(hour=9, minute=55)
    end = end or NOW.replace(hour=11, minute=20)
    return CourseOccurrence("synthetic", start, name, start, end, location)


def view(mode="current", courses=None, name="示例群友", now=NOW):
    return TimetableView(mode, now, (MemberView("111", name, (course(),) if courses is None else courses),))


class TimetableRenderTest(unittest.TestCase):
    def test_current_png_is_rgb_bounded_and_in_memory(self):
        plan = build_plan(view())
        with tempfile.TemporaryDirectory() as directory:
            before = list(Path(directory).iterdir())
            raw = render_page(plan, 0)
            self.assertEqual(list(Path(directory).iterdir()), before)
        self.assertLess(len(raw), plan.limits.max_image_bytes)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.width, 1560)
            self.assertLessEqual(image.height, 2700)
            image.verify()

    def test_png_save_receives_memory_buffer_not_a_path(self):
        original = Image.Image.save
        destinations = []
        def save(image, fp, *args, **kwargs):
            destinations.append(fp)
            self.assertIsInstance(fp, io.BytesIO)
            return original(image, fp, *args, **kwargs)
        with patch.object(Image.Image, "save", new=save):
            render_page(build_plan(view()), 0)
        self.assertTrue(destinations)
        self.assertTrue(all(buffer.closed for buffer in destinations))

    def test_today_three_states_and_14_30_boundary(self):
        morning = course("早课", NOW.replace(hour=8, minute=0), NOW.replace(hour=9, minute=25))
        afternoon = course("下午课", NOW.replace(hour=14, minute=30), NOW.replace(hour=15, minute=55))
        items = (morning, course(), afternoon)
        plan = build_plan(view("today", items))
        self.assertEqual([course_state(row.course, NOW) for row in plan.pages[0].rows],
                         ["ended", "current", "upcoming"])
        for at, expected in [(afternoon.starts_at - timedelta(seconds=1), "upcoming"),
                             (afternoon.starts_at, "current"),
                             (afternoon.ends_at - timedelta(seconds=1), "current"),
                             (afternoon.ends_at, "ended")]:
            self.assertEqual(course_state(afternoon, at), expected)
        self.assertNotEqual(render_page(plan, 0), render_page(build_plan(view("today", items, now=afternoon.starts_at)), 0))

    def test_progress_and_less_than_one_minute(self):
        self.assertEqual(course_progress(course(), NOW), (25 / 85, 25, "60 分钟"))
        self.assertEqual(course_progress(course(), course().starts_at)[0], 0)
        self.assertEqual(course_progress(course(), course().ends_at)[0], 1)
        self.assertEqual(course_progress(course(), course().ends_at - timedelta(seconds=1))[2], "不足 1 分钟")

    def test_today_empty_and_current_idle_have_valid_pages(self):
        for mode in ("today", "tomorrow", "current"):
            plan = build_plan(view(mode, ()))
            self.assertIsNone(plan.pages[0].rows[0].course)
            self.assertTrue(render_page(plan, 0).startswith(b"\x89PNG"))

    def test_overlap_preserves_each_course_and_member_identity(self):
        plan = build_plan(view(courses=(course("甲课"), course("乙课"))))
        rows = [row for page in plan.pages for row in page.rows]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.course.name for row in rows}, {"甲课", "乙课"})
        self.assertTrue(all(row.member.user_id == "111" for row in rows))

    def test_long_chinese_latin_names_and_locations_wrap_without_truncation(self):
        title = "Linux操作系统及Python编程基础" * 3
        location = "教学楼很长的地点" * 7
        plan = build_plan(view(courses=(course(title, location=location),), name="长昵称" * 15))
        row = plan.pages[0].rows[0]
        self.assertEqual("".join(row.title_lines), title)
        self.assertEqual("".join(row.location_lines), location)
        self.assertEqual("".join(row.name_lines), "长昵称" * 15)
        self.assertGreater(len(row.title_lines), 1)
        self.assertTrue(render_page(plan, 0))

    def test_very_long_latin_word_is_wrapped(self):
        title = "Python" * 30
        row = build_plan(view(courses=(course(title),))).pages[0].rows[0]
        self.assertEqual("".join(row.title_lines), title)
        self.assertGreater(len(row.title_lines), 1)

    def test_pagination_is_preflighted_and_omits_nobody(self):
        members = tuple(MemberView(str(100 + i), f"成员{i}", ()) for i in range(12))
        plan = build_plan(TimetableView("current", NOW, members), RenderLimits(max_page_height=1000))
        self.assertGreater(len(plan.pages), 1)
        self.assertEqual([row.member.user_id for page in plan.pages for row in page.rows],
                         [member.user_id for member in members])
        for index, page in enumerate(plan.pages):
            self.assertLessEqual(page.height, 1000)
            self.assertTrue(render_page(plan, index))

    def test_page_item_pixel_and_encoded_size_caps(self):
        many = TimetableView("current", NOW, tuple(MemberView(str(100 + i), "群友", ()) for i in range(20)))
        with self.assertRaises(TimetableRenderError):
            build_plan(many, RenderLimits(max_pages=1))
        with self.assertRaises(TimetableRenderError):
            build_plan(many, RenderLimits(max_items=5))
        with self.assertRaises(TimetableRenderError):
            build_plan(view(), RenderLimits(max_pixels=10))
        with self.assertRaises(TimetableRenderError):
            render_page(build_plan(view(), RenderLimits(max_image_bytes=100)), 0)

    def test_cross_midnight_dates_and_seconds_are_retained(self):
        item = course("夜课", NOW.replace(day=7, hour=23, minute=30, second=15),
                      NOW.replace(hour=1, minute=0))
        self.assertEqual(course_time(item), "09-07 23:30:15–09-08 01:00")
        self.assertTrue(render_page(build_plan(view("today", (item,))), 0))

    def test_other_timezone_normalizes_to_shanghai(self):
        from datetime import timezone
        item = course()
        utc = replace(item, starts_at=item.starts_at.astimezone(timezone.utc),
                      ends_at=item.ends_at.astimezone(timezone.utc))
        plan = build_plan(view(courses=(utc,), now=NOW.astimezone(timezone.utc)))
        self.assertEqual(plan.view.now.hour, 10)
        self.assertEqual(plan.pages[0].rows[0].course.starts_at.hour, 9)

    def test_invalid_avatar_falls_back_to_default(self):
        plan = build_plan(view())
        self.assertEqual(render_page(plan, 0), render_page(plan, 0, {"111": b"not an image"}))
        with io.BytesIO() as output, Image.new("RGB", (160, 160), "red") as avatar:
            avatar.save(output, format="PNG")
            self.assertNotEqual(render_page(plan, 0), render_page(plan, 0, {"111": output.getvalue()}))

    def test_missing_font_can_be_reported_without_logging_private_text(self):
        with patch.dict("os.environ", {"TIMETABLE_FONT_REGULAR": "missing-private-font"}):
            with self.assertRaisesRegex(TimetableRenderError, "字体不可用"):
                build_plan(view())

    def test_oversized_personal_header_is_rejected_before_font_measurement(self):
        with patch("plugins.timetable_render.discover_fonts") as fonts:
            with self.assertRaises(TimetableRenderError):
                build_plan(view("today", name="长" * 141))
            fonts.assert_not_called()

    def test_invalid_personal_scope_and_time_are_rejected(self):
        for bad in (replace(view("today"), members=view().members * 2),
                    replace(view(), now=NOW.replace(tzinfo=None)),
                    view(courses=(course(end=NOW.replace(hour=9)),))):
            with self.assertRaises(TimetableRenderError):
                build_plan(bad)

    def test_control_chars_do_not_add_layout_or_cq_segments(self):
        name = "名字\x00\n[CQ:at,qq=all]"
        plan = build_plan(view(name=name))
        self.assertEqual(clean_text(name), "名字 [CQ:at,qq=all]")
        self.assertNotIn("\n", "".join(plan.pages[0].rows[0].name_lines))
        self.assertTrue(render_page(plan, 0))


if __name__ == "__main__":
    unittest.main()
