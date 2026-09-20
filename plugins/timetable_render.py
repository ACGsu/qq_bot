"""Bounded, network-free Pillow timetable layouts; PNG output stays in memory."""
from __future__ import annotations

import io
import math
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .timetable_ics import CourseOccurrence, SHANGHAI

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # Missing optional image support must not disable ICS imports.
    Image = ImageDraw = ImageFont = ImageOps = None

WIDTH = 1040
COLORS = {
    "white": "#FFFFFF", "ink": "#233339", "muted": "#79878C",
    "soft": "#536369", "teal": "#078B7F", "mint": "#D9F1E9",
    "line": "#E6ECEB", "purple": "#6E54B5", "purple_dark": "#56408B",
    "purple_light": "#F6F3FC", "purple_line": "#E4DCF2", "lilac": "#C5B8E3",
    "gray": "#F6F8F8", "gray_tag": "#EBEFF0", "green_tag": "#E5F3EC",
    "green": "#277B64",
}


class TimetableRenderError(RuntimeError):
    """Safe failure: callers use the existing complete text representation."""


@dataclass(frozen=True)
class RenderLimits:
    scale: float = 1.5
    max_page_height: int = 1800  # Logical pixels; output is 1560 x at most 2700.
    max_pages: int = 8
    max_items: int = 512
    max_pixels: int = 4_500_000
    max_image_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        if not 0 < self.scale <= 2 or min(self.max_page_height, self.max_pages,
                self.max_items, self.max_pixels, self.max_image_bytes) <= 0:
            raise ValueError("Invalid timetable render limits")


@dataclass(frozen=True)
class MemberView:
    user_id: str
    name: str
    courses: tuple[CourseOccurrence, ...]


@dataclass(frozen=True)
class TimetableView:
    mode: str  # current / today
    now: datetime
    members: tuple[MemberView, ...]


@dataclass(frozen=True)
class LayoutRow:
    member: MemberView
    course: CourseOccurrence | None
    name_lines: tuple[str, ...]
    title_lines: tuple[str, ...]
    time_lines: tuple[str, ...]
    location_lines: tuple[str, ...]
    height: int


@dataclass(frozen=True)
class LayoutPage:
    rows: tuple[LayoutRow, ...]
    height: int


@dataclass(frozen=True)
class RenderPlan:
    view: TimetableView
    pages: tuple[LayoutPage, ...]
    limits: RenderLimits
    font_paths: tuple[str, str]
    content_top: int
    profile_lines: tuple[str, ...]


def clean_text(value: str) -> str:
    # ICS and nicknames are text, never paths, markup, or drawing instructions.
    return " ".join("".join(" " if unicodedata.category(c).startswith("C") else c
                            for c in value).split())


def course_state(course: CourseOccurrence, now: datetime) -> str:
    return "ended" if course.ends_at <= now else "current" if course.starts_at <= now else "upcoming"


def course_progress(course: CourseOccurrence, now: datetime) -> tuple[float, int, str]:
    duration = (course.ends_at - course.starts_at).total_seconds()
    elapsed = max(0.0, min(duration, (now - course.starts_at).total_seconds()))
    remaining = max(0.0, (course.ends_at - now).total_seconds())
    label = "不足 1 分钟" if 0 < remaining < 60 else f"{int(remaining // 60)} 分钟"
    return elapsed / duration, int(elapsed // 60), label


def clock(when: datetime) -> str:
    return when.strftime("%H:%M:%S" if when.second else "%H:%M")


def course_time(course: CourseOccurrence) -> str:
    if course.starts_at.date() == course.ends_at.date():
        return f"{clock(course.starts_at)}–{clock(course.ends_at)}"
    return f"{course.starts_at:%m-%d} {clock(course.starts_at)}–{course.ends_at:%m-%d} {clock(course.ends_at)}"


def discover_fonts() -> tuple[str, str]:
    regular = os.environ.get("TIMETABLE_FONT_REGULAR")
    bold = os.environ.get("TIMETABLE_FONT_BOLD")
    if regular:
        paths = (Path(regular), Path(bold or regular))
        if not all(p.is_file() for p in paths):
            raise TimetableRenderError("课表字体不可用")
        return tuple(str(p) for p in paths)
    candidates = [
        ("/usr/share/fonts/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ]
    if os.name == "nt":  # Development only: never redistribute Windows fonts.
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        candidates.append((str(fonts / "msyh.ttc"), str(fonts / "msyhbd.ttc")))
    for regular, bold_path in candidates:
        if Path(regular).is_file():
            return regular, bold or (bold_path if Path(bold_path).is_file() else regular)
    raise TimetableRenderError("未找到中文字体")


class _Fonts:
    def __init__(self, paths: tuple[str, str], scale: float):
        if ImageFont is None:
            raise TimetableRenderError("Pillow 不可用")
        self.paths, self.scale = paths, scale
        self.cache: dict[tuple[int, bool], object] = {}
        self.glyphs: dict[str, bool] = {}
        self.missing = self._mask("\uffff")

    def get(self, size: int, bold: bool = False):
        key = (size, bold)
        if key not in self.cache:
            if len(self.cache) >= 40:
                self.cache.clear()
            try:
                # Noto CJK's SC face is index 2; Latin metrics are shared by faces.
                index = 2 if "NotoSansCJK" in self.paths[bold] else 0
                self.cache[key] = ImageFont.truetype(self.paths[bold], round(size * self.scale), index=index)
            except (OSError, ValueError) as exc:
                raise TimetableRenderError("中文字体加载失败") from exc
        return self.cache[key]

    def _mask(self, character: str):
        mask = self.get(24).getmask(character)
        return mask.size, bytes(mask)

    def safe(self, text: str) -> str:
        result = []
        for c in clean_text(text):
            if c not in self.glyphs:
                if len(self.glyphs) >= 2048:
                    self.glyphs.clear()
                self.glyphs[c] = c.isspace() or self._mask(c) != self.missing
            result.append(c if self.glyphs[c] else "□")
        return "".join(result)

    def width(self, text: str, size: int, bold: bool = False) -> float:
        return self.get(size, bold).getlength(text) / self.scale

    def wrap(self, text: str, size: int, width: int, bold: bool = False) -> tuple[str, ...]:
        text = self.safe(text)
        tokens = re.findall(r"[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*|.", text)
        lines, line = [], ""
        for token in tokens:
            # Very long Latin words are also wrapped instead of overflowing.
            parts = list(token) if self.width(token, size, bold) > width else [token]
            for part in parts:
                if line and self.width(line + part, size, bold) > width:
                    lines.append(line.rstrip())
                    line = part.lstrip()
                else:
                    line += part
        if line.strip():
            lines.append(line.rstrip())
        if any(self.width(line, size, bold) > width + 1 for line in lines):
            raise TimetableRenderError("文字超出可绘制范围")
        return tuple(lines)


_local = threading.local()


def _fonts(paths, scale):
    key = (paths, scale)
    if getattr(_local, "key", None) != key:
        _local.fonts = _Fonts(paths, scale)
        _local.key = key
    return _local.fonts


def build_plan(view: TimetableView, limits: RenderLimits = RenderLimits(),
               font_paths: tuple[str, str] | None = None) -> RenderPlan:
    """Measure the complete response before sending the first page. No raster yet."""
    if view.mode not in {"current", "today"} or not view.members or view.now.utcoffset() is None:
        raise TimetableRenderError("无效的课表展示数据")
    if view.mode == "today" and len(view.members) != 1:
        raise TimetableRenderError("个人课表只能包含本人")
    if sum(max(1, len(m.courses)) for m in view.members) > limits.max_items:
        raise TimetableRenderError("图片课表内容过多")
    # Validate even the personal header before font measurement of user text.
    for member in view.members:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", member.user_id) or len(member.name) > 140:
            raise TimetableRenderError("无效的成员展示数据")
    view = replace(view, now=view.now.astimezone(SHANGHAI))
    paths = font_paths or discover_fonts()
    fonts = _fonts(paths, limits.scale)
    rows = []
    profile = ()
    top = 344
    if view.mode == "today":
        profile = fonts.wrap(view.members[0].name, 30, 550, True)
        top = 294 + max(128, len(profile) * 40 + 68) + 40
    for member in view.members:
        names = fonts.wrap(member.name or f"QQ {member.user_id}", 26, 148, True)
        for course in sorted(member.courses, key=lambda c: (c.starts_at, c.ends_at, c.name)) or [None]:
            if course is not None:
                if (len(course.name) > 200 or len(course.location) > 200
                        or course.starts_at.utcoffset() is None or course.ends_at.utcoffset() is None
                        or course.ends_at <= course.starts_at):
                    raise TimetableRenderError("无效的课程展示数据")
                course = replace(course, starts_at=course.starts_at.astimezone(SHANGHAI),
                                 ends_at=course.ends_at.astimezone(SHANGHAI))
                if view.mode == "current" and course_state(course, view.now) != "current":
                    raise TimetableRenderError("当前课程与查询时间不一致")
                titles = fonts.wrap(course.name, 34 if view.mode == "current" else 32,
                                    528 if view.mode == "current" else 672, True)
                locations = fonts.wrap(course.location, 22, 498 if view.mode == "current" else 640)
                times = fonts.wrap(course_time(course), 28, 528, True)
            else:
                titles, locations, times = (), (), ()
            if view.mode == "current":
                height = 80 + len(titles) * 44 + 12 + len(times) * 36
                height += (16 + len(locations) * 30 if locations else 0) + 76
                height = max(height if course else 174, len(names) * 36 + 76, 180)
            else:
                height = 78 + len(titles) * 44 + (16 + len(locations) * 30 if locations else 0) + 32
                if course and course_state(course, view.now) == "current":
                    height += 67
                height = max(height, 178)
            rows.append(LayoutRow(member, course, names, titles, times, locations, height))
    pages, current, used = [], [], top
    for row in rows:
        if top + row.height + 156 > limits.max_page_height:
            raise TimetableRenderError("单条课表内容过长")
        if current and used + row.height + 156 > limits.max_page_height:
            pages.append(LayoutPage(tuple(current), used - 24 + 156))
            current, used = [], top
        current.append(row)
        used += row.height + 24
    if current:
        pages.append(LayoutPage(tuple(current), used - 24 + 156))
    if len(pages) > limits.max_pages:
        raise TimetableRenderError("图片页数超过单次上限")
    for page in pages:
        if round(WIDTH * limits.scale) * round(page.height * limits.scale) > limits.max_pixels:
            raise TimetableRenderError("图片像素数超过上限")
    return RenderPlan(view, tuple(pages), limits, paths, top, profile)


class _LimitedBuffer(io.BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise TimetableRenderError("PNG 大小超过单页上限")
        return super().write(data)


class _Canvas:
    def __init__(self, plan, height):
        self.scale = plan.limits.scale
        self.fonts = _fonts(plan.font_paths, self.scale)
        self.image = Image.new("RGB", (round(WIDTH * self.scale), round(height * self.scale)), COLORS["white"])
        self.draw = ImageDraw.Draw(self.image)

    def coords(self, box):
        return tuple(round(n * self.scale) for n in box)

    def text(self, x, y, text, size=24, color="ink", bold=False, right=False):
        if right:
            x -= self.fonts.width(text, size, bold)
        self.draw.text(self.coords((x, y)), text, font=self.fonts.get(size, bold),
                       fill=COLORS.get(color, color), anchor="lt")

    def lines(self, x, y, lines, size, leading, color="ink", bold=False):
        for index, text in enumerate(lines):
            self.text(x, y + index * leading, text, size, color, bold)

    def rect(self, box, color, radius=0, outline=None):
        fill = COLORS.get(color, color)
        border = COLORS.get(outline, outline)
        if radius:
            self.draw.rounded_rectangle(self.coords(box), round(radius * self.scale), fill=fill,
                                        outline=border, width=max(1, round(self.scale)))
        else:
            self.draw.rectangle(self.coords(box), fill=fill)

    def line(self, points, color="line", width=1):
        self.draw.line([self.coords(p) for p in points], COLORS[color], max(1, round(width * self.scale)))

    def ellipse(self, box, color):
        self.draw.ellipse(self.coords(box), fill=COLORS.get(color, color))

    def polygon(self, points, color):
        self.draw.polygon([self.coords(p) for p in points], fill=COLORS[color])

    def pill(self, x, y, text, bg, fg, size=21, height=38):
        w = self.fonts.width(text, size, True) + 30
        self.rect((x, y, x + w, y + height), bg, height / 2)
        self.text(x + 15, y + (height - size) / 2 - 1, text, size, fg, True)

    def avatar(self, data, x, y, size):
        pixels = round(size * self.scale)
        self.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), "line")
        if data:
            try:
                with io.BytesIO(data) as buf, Image.open(buf, formats=("PNG",)) as source:
                    if source.width * source.height > 256 * 256:
                        raise ValueError("Avatar is not normalized")
                    with source.convert("RGB") as rgb, ImageOps.fit(rgb, (pixels, pixels), Image.Resampling.LANCZOS) as face:
                        with Image.new("L", (pixels, pixels), 0) as mask:
                            ImageDraw.Draw(mask).ellipse((0, 0, pixels - 1, pixels - 1), fill=255)
                            self.image.paste(face, self.coords((x, y)), mask)
                return
            except (OSError, ValueError, SyntaxError):
                pass
        self.ellipse((x, y, x + size, y + size), "mint")
        self.ellipse((x + size * .35, y + size * .20, x + size * .65, y + size * .50), "teal")
        self.draw.pieslice(self.coords((x + size * .18, y + size * .44, x + size * .82, y + size * 1.04)),
                           180, 360, fill=COLORS["teal"])

    def location(self, x, y, lines):
        if not lines:
            return
        self.ellipse((x + 4, y + 2, x + 16, y + 14), "muted")
        self.ellipse((x + 7, y + 5, x + 13, y + 11), "white")
        self.polygon([(x + 5, y + 11), (x + 15, y + 11), (x + 10, y + 22)], "muted")
        self.lines(x + 30, y, lines, 22, 30, "soft")

    def progress(self, x, y, right, course, now):
        ratio, elapsed, remaining = course_progress(course, now)
        self.rect((x, y, right, y + 5), "purple_line", 2)
        if ratio > 0:
            self.rect((x, y, x + (right - x) * ratio, y + 5), "purple", 2)
        self.text(x, y + 20, f"已上课 {elapsed} 分钟", 19, "muted")
        self.text(right, y + 20, f"距下课 {remaining}", 19, "purple_dark", right=True)


def _header(c, plan, avatars):
    view = plan.view
    current = view.mode == "current"
    c.polygon([(48, 44), (94, 44), (94, 62), (66, 62), (66, 90), (48, 90)], "teal")
    c.text(116, 51, "课表助手", 22, "teal", True)
    command = "/课ing" if current else "/今日课程"
    c.pill(984 - c.fonts.width(command, 23, True) - 30, 44, command, "gray", "soft", 23, 42)
    title = "群友在上什么课？" if current else "今日课程"
    c.rect((57, 164, 62 + c.fonts.width(title, 52, True), 180), "mint")
    c.text(56, 124, title, 52, bold=True)
    weekdays = "一二三四五六日"
    c.text(56, 214, f"{view.now:%Y年%m月%d日}  星期{weekdays[view.now.weekday()]}", 24, "soft")
    c.text(984, 215, f"{view.now:%H:%M:%S} · 查询时刻", 22, "muted", right=True)
    c.line([(56, 264), (984, 264)])
    if current:
        total, active = len(view.members), sum(bool(m.courses) for m in view.members)
        c.text(56, 295, f"本群已导入 {total} 人  ·  {active} 人正在上课", 22, "soft")
    else:
        member = view.members[0]
        bottom = plan.content_top - 40
        c.rect((56, 294, 984, bottom), "#F5F9F7", 20)
        c.avatar(avatars.get(member.user_id), 84, (294 + bottom) / 2 - 40, 80)
        c.lines(192, 318, plan.profile_lines, 30, 40, bold=True)
        c.text(192, 322 + len(plan.profile_lines) * 40, "只展示你自己的课表", 21, "muted")
        c.line([(798, 318), (798, bottom - 24)])
        c.text(873, (294 + bottom) / 2 - 45, str(len(member.courses)), 45, "teal", True, right=True)
        c.text(834, (294 + bottom) / 2 + 21, "门课程", 20, "muted")


def _current_row(c, row, y, now, avatars):
    end = y + row.height
    c.rect((56, y, 984, end), "#FCFBFE" if row.course else "gray", 24,
           "purple_line" if row.course else "line")
    mid = (y + end) / 2
    c.avatar(avatars.get(row.member.user_id), 84, mid - 48, 96)
    name_top = mid - (len(row.name_lines) * 36 + 24) / 2
    c.lines(202, name_top, row.name_lines, 26, 36, bold=True)
    label = f"同时 {len(row.member.courses)} 门课" if len(row.member.courses) > 1 else "已导入课表"
    c.text(202, name_top + len(row.name_lines) * 36 + 7, label, 18, "muted")
    c.polygon([(356, mid - 22), (369, mid - 22), (391, mid),
               (369, mid + 22), (356, mid + 22), (378, mid)], "lilac")
    x = 420
    if not row.course:
        c.pill(x, mid - 40, "无课程", "gray_tag", "soft")
        c.text(x, mid + 16, "当前没有课", 30, "muted")
        return
    c.pill(x, y + 24, "上课中", "purple", "white")
    text_y = y + 80
    c.lines(x, text_y, row.title_lines, 34, 44, "purple_dark", True)
    time_y = text_y + len(row.title_lines) * 44 + 12
    c.lines(x, time_y, row.time_lines, 28, 36, "purple_dark", True)
    next_y = time_y + len(row.time_lines) * 36
    if row.location_lines:
        next_y += 16
        c.location(x, next_y, row.location_lines)
        next_y += len(row.location_lines) * 30
    c.progress(x, next_y + 20, 952, row.course, now)


def _today_row(c, row, y, now):
    course = row.course
    if not course:
        c.rect((56, y, 984, y + row.height), "gray", 20, "line")
        c.text(96, y + 45, "今日无课程", 34, "teal", True)
        c.text(96, y + 104, "今天没有安排课程。", 24, "muted")
        return
    state = course_state(course, now)
    active, ended = state == "current", state == "ended"
    fg = "muted" if ended else "purple_dark" if active else "ink"
    c.text(56, y + 22, clock(course.starts_at), 28, fg, True)
    c.text(56, y + 82, clock(course.ends_at), 25, "muted")
    if course.starts_at.date() != now.date():
        c.text(56, y + 57, f"{course.starts_at:%m-%d} 开始", 17, "muted")
    if course.ends_at.date() != now.date():
        c.text(56, y + 117, f"{course.ends_at:%m-%d} 结束", 17, "muted")
    if active:
        c.ellipse((198, y + 28, 226, y + 56), "purple_line")
    c.ellipse((206, y + 36, 218, y + 48), "muted" if ended else "purple" if active else "teal")
    c.rect((248, y, 984, y + row.height), "purple_light" if active else "gray" if ended else "white",
           20, "purple_line" if active else "line")
    if active:
        c.rect((249, y + 30, 253, y + row.height - 30), "purple", 2)
    c.pill(280, y + 24, "已结束" if ended else "上课中" if active else "未开始",
           "gray_tag" if ended else "purple" if active else "green_tag",
           "muted" if ended else "white" if active else "green", 20, 35)
    duration = math.ceil((course.ends_at - course.starts_at).total_seconds() / 60)
    c.text(952, y + 29, f"{duration} 分钟", 20, "muted", right=True)
    c.lines(280, y + 78, row.title_lines, 32, 44, "soft" if ended else fg, True)
    next_y = y + 78 + len(row.title_lines) * 44
    if row.location_lines:
        next_y += 16
        c.location(280, next_y, row.location_lines)
        next_y += len(row.location_lines) * 30
    if active:
        c.progress(280, next_y + 22, 952, course, now)


def render_page(plan: RenderPlan, index: int, avatars: Mapping[str, bytes | None] | None = None) -> bytes:
    """One page at a time. All images and encoding buffers close before returning."""
    page = plan.pages[index]
    avatars = avatars or {}
    c = _Canvas(plan, page.height)
    try:
        _header(c, plan, avatars)
        y = plan.content_top
        if plan.view.mode == "today" and page.rows[0].course:
            last_y = y + sum(row.height + 24 for row in page.rows[:-1])
            c.line([(212, y + 42), (212, last_y + 42)], width=2)
        for row in page.rows:
            if plan.view.mode == "current":
                _current_row(c, row, y, plan.view.now, avatars)
            else:
                _today_row(c, row, y, plan.view.now)
            y += row.height + 24
        footer = page.height - 128
        c.line([(56, footer), (984, footer)])
        c.text(56, footer + 28, "课表助手", 22, "teal", True)
        c.text(176, footer + 29, "使用 @bot /导入课表 添加你的课表", 20, "soft")
        c.text(56, footer + 73, f"查询时间 {plan.view.now:%Y-%m-%d %H:%M:%S} · 北京时间", 18, "muted")
        c.text(912, footer + 73, f"{index + 1} / {len(plan.pages)}", 19, "muted", right=True)
        c.polygon([(960, footer + 54), (984, footer + 54), (984, footer + 100),
                   (938, footer + 100), (938, footer + 79), (960, footer + 79)], "teal")
        with _LimitedBuffer(plan.limits.max_image_bytes) as output:
            c.image.save(output, format="PNG", compress_level=3)
            return output.getvalue()
    finally:
        c.image.close()
