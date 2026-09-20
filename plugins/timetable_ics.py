"""Bounded ICS-to-course parsing; no persistence, network access or calendar actions.

icalendar decodes RFC 5545 content lines; dateutil expands validated, finite rules.
The bot runs this module in a short-lived worker so malformed calendars cannot
hold the WebSocket event loop (or an unkillable parser thread) indefinitely.
"""

import asyncio
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule
from dateutil.tz import datetime_exists
from icalendar import Calendar
from icalendar.parser import Contentlines

from .timetable_files import MAX_ICS_BYTES, TimetableFileError, inspect_calendar_payload


SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
MAX_SOURCE_EVENTS = 512
MAX_OCCURRENCES = 10_000
MAX_SPAN_DAYS = 730
PARSE_TIMEOUT_SECONDS = 10
MAX_RESULT_BYTES = 32 * 1024 * 1024
FLOATING_TIME_WARNING = "未标注时区的时间已按 Asia/Shanghai（北京时间）解释。"
WEEKDAYS = dict(zip(("MO", "TU", "WE", "TH", "FR", "SA", "SU"),
                    (rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR, rrule.SA, rrule.SU)))
FREQUENCIES = {name: getattr(rrule, name) for name in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")}
RULE_KEYS = {"FREQ", "COUNT", "UNTIL", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}


class TimetableParseError(ValueError):
    """A user-safe message: never include calendar contents or library errors."""


@dataclass(frozen=True)
class CourseOccurrence:
    uid: str
    recurrence_id: datetime  # Original instance start, in UTC, even after rescheduling.
    name: str
    starts_at: datetime      # All public course times use Asia/Shanghai.
    ends_at: datetime
    location: str = ""


@dataclass(frozen=True)
class ParsedTimetable:
    source_event_count: int
    series_count: int
    occurrences: tuple[CourseOccurrence, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Course:
    uid: str
    name: str
    location: str
    start: datetime
    duration: timedelta


def _single(component: Any, key: str, *, required: bool = False) -> Any:
    value = component.get(key)
    if isinstance(value, list):
        raise TimetableParseError(f"同一个事件包含重复的 {key} 字段，请修正后重新导出。")
    if required and value is None:
        raise TimetableParseError(f"课程事件缺少 {key} 字段，请重新导出完整课表。")
    return value


def _text(component: Any, key: str, *, required: bool = False, default: str = "", limit: int = 200) -> str:
    value = _single(component, key, required=required)
    result = default if value is None else str(value).strip()
    if required and not result:
        raise TimetableParseError(f"课程事件的 {key} 不能为空。")
    if len(result) > limit:
        raise TimetableParseError(f"课程事件的 {key} 过长，请精简后重新导出。")
    return result


def _time(value: Any, label: str, warnings: set[str]) -> datetime:
    when = getattr(value, "dt", None)
    if not isinstance(when, datetime):
        raise TimetableParseError(f"{label} 必须包含具体时间；暂不支持全天事件或 RDATE PERIOD。")
    tzid = value.params.get("TZID")
    if tzid is not None:
        if not isinstance(tzid, str) or len(tzid) > 128:
            raise TimetableParseError("TZID 时区无效，请使用 Asia/Shanghai 等 IANA 时区或 UTC。")
        try:
            zone = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            raise TimetableParseError("TZID 时区无法识别；不支持自定义时区，请使用 IANA 时区或 UTC。") from None
        # Use IANA data, not arbitrary embedded VTIMEZONE definitions.
        when = when.replace(tzinfo=zone, fold=0)
    elif when.tzinfo is None:
        warnings.add(FLOATING_TIME_WARNING)
        when = when.replace(tzinfo=SHANGHAI)
    if not datetime_exists(when):
        raise TimetableParseError(f"{label} 位于夏令时跳过的时段，请修正时间。")
    return when


def _duration(component: Any, start: datetime, warnings: set[str], default: timedelta | None = None) -> timedelta:
    end_value = _single(component, "DTEND")
    duration_value = _single(component, "DURATION")
    if end_value is not None and duration_value is not None:
        raise TimetableParseError("课程不能同时指定 DTEND 和 DURATION。")
    if end_value is not None:
        duration = _time(end_value, "DTEND", warnings).astimezone(UTC) - start.astimezone(UTC)
    elif duration_value is not None:
        duration = getattr(duration_value, "dt", None)
    else:
        duration = default
    if not isinstance(duration, timedelta):
        raise TimetableParseError("课程缺少有效的下课时间 DTEND 或时长 DURATION。")
    if not timedelta(0) < duration < timedelta(days=1):
        raise TimetableParseError("单次课程时长必须大于 0 且小于 24 小时；下课时间必须晚于上课时间。")
    return duration


def _course(component: Any, warnings: set[str], base: _Course | None = None) -> _Course:
    uid = _text(component, "UID", required=True, limit=512)
    start = _time(_single(component, "DTSTART", required=True), "DTSTART", warnings)
    name = _text(component, "SUMMARY", required=base is None, default=base.name if base else "")
    if not name:
        raise TimetableParseError("课程名 SUMMARY 不能为空。")
    location = _text(component, "LOCATION", default=base.location if base else "")
    return _Course(uid, name, location, start, _duration(component, start, warnings, base.duration if base else None))


def _dates(component: Any, key: str, warnings: set[str]) -> set[datetime]:
    values = component.get(key, [])
    if not isinstance(values, list):
        values = [values]
    result: set[datetime] = set()
    count = 0
    for value in values:
        if not hasattr(value, "dts"):
            raise TimetableParseError(f"{key} 日期列表无效。")
        for entry in value.dts:
            count += 1
            if count > MAX_OCCURRENCES:
                raise TimetableParseError(f"{key} 日期数量超过 {MAX_OCCURRENCES} 条上限。")
            result.add(_time(entry, key, warnings).astimezone(UTC))
    return result


def _rule_values(rule: Any, key: str, minimum: int, maximum: int, *, default: int | None = None,
                 single: bool = False, nonzero: bool = False) -> list[int]:
    values = rule.get(key, [default] if default is not None else [])
    if not isinstance(values, list) or len(values) > 64 or (single and len(values) != 1):
        raise TimetableParseError(f"RRULE 的 {key} 格式无效或数量过多。")
    result = []
    for value in values:
        if not isinstance(value, int) or not minimum <= value <= maximum or (nonzero and value == 0):
            raise TimetableParseError(f"RRULE 的 {key} 超出支持范围。")
        result.append(int(value))
    return result


def _expand(component: Any, start: datetime, deadline: datetime) -> set[datetime]:
    rule = _single(component, "RRULE")
    if rule is None:
        return {start.astimezone(UTC)}
    if not hasattr(rule, "keys") or set(rule.keys()) - RULE_KEYS:
        raise TimetableParseError("RRULE 包含暂不支持的规则字段；请导出按日、周、月或年重复的课表。")
    freq = rule.get("FREQ", [])
    if len(freq) != 1 or str(freq[0]) not in FREQUENCIES:
        raise TimetableParseError("仅支持 DAILY、WEEKLY、MONTHLY、YEARLY；不支持按秒、分钟或小时重复。")
    freq_name = str(freq[0])
    if ("COUNT" in rule) == ("UNTIL" in rule):
        raise TimetableParseError("每条重复规则必须且只能包含 COUNT 或 UNTIL，不能无限重复。")
    count = _rule_values(rule, "COUNT", 1, MAX_OCCURRENCES, single=True)[0] if "COUNT" in rule else None
    until = deadline
    if "UNTIL" in rule:
        values = rule["UNTIL"]
        if len(values) != 1 or not isinstance(values[0], datetime):
            raise TimetableParseError("带时间课程的 UNTIL 必须包含具体时间，不能仅填写日期。")
        until = values[0]
        original_start = component["DTSTART"].dt
        if original_start.tzinfo is None and not component["DTSTART"].params.get("TZID"):
            if until.tzinfo is not None:
                raise TimetableParseError("无时区 DTSTART 应搭配无时区 UNTIL。")
            until = until.replace(tzinfo=start.tzinfo)
        elif until.tzinfo is None or until.utcoffset() != timedelta(0):
            raise TimetableParseError("带时区 DTSTART 的 UNTIL 必须使用 UTC（以 Z 结尾）。")
        until = until.astimezone(UTC)
        if until < start.astimezone(UTC):
            raise TimetableParseError("UNTIL 不能早于首次上课时间。")
        if until > deadline:
            raise TimetableParseError(f"课表日期范围超过 {MAX_SPAN_DAYS} 天上限，请按学期导出。")
    options: dict[str, Any] = {
        "freq": FREQUENCIES[freq_name], "dtstart": start, "until": until,
        "interval": _rule_values(rule, "INTERVAL", 1, 366, default=1, single=True)[0],
    }
    for key, lower, upper in (("BYMONTH", 1, 12), ("BYMONTHDAY", -31, 31), ("BYSETPOS", -366, 366)):
        if key in rule:
            options[key.lower()] = _rule_values(rule, key, lower, upper, nonzero=True)
    if "BYMONTHDAY" in rule and freq_name == "WEEKLY":
        raise TimetableParseError("WEEKLY 不能搭配 BYMONTHDAY，请修正重复规则。")
    if "BYSETPOS" in rule and not any(key in rule for key in ("BYDAY", "BYMONTHDAY", "BYMONTH")):
        raise TimetableParseError("BYSETPOS 必须搭配 BYDAY、BYMONTHDAY 或 BYMONTH。")
    if "BYDAY" in rule:
        if not 1 <= len(rule["BYDAY"]) <= 64:
            raise TimetableParseError("BYDAY 数量无效或过多。")
        days = []
        for value in rule["BYDAY"]:
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", str(value))
            if match is None:
                raise TimetableParseError("BYDAY 星期格式无效。")
            ordinal, name = match.groups()
            day = WEEKDAYS[name]
            if ordinal is not None:
                number = int(ordinal)
                if number == 0 or abs(number) > 53 or freq_name not in {"MONTHLY", "YEARLY"}:
                    raise TimetableParseError("带序号的 BYDAY 仅支持按月或年重复，序号不能为 0。")
                day = day(number)
            days.append(day)
        options["byweekday"] = days
    if "WKST" in rule:
        values = rule["WKST"]
        if len(values) != 1 or str(values[0]) not in WEEKDAYS:
            raise TimetableParseError("WKST 星期格式无效。")
        options["wkst"] = WEEKDAYS[str(values[0])]
    else:
        options["wkst"] = rrule.MO  # Independent of the host's calendar.firstweekday().
    result: set[datetime] = set()
    # Even COUNT rules get a bounded search horizon. An impossible rule (e.g.
    # February 30) must not search until year 9999 or be silently truncated.
    for when in rrule.rrule(**options):
        if not datetime_exists(when):  # RFC 5545: nonexistent local instances don't count.
            continue
        stamp = when.astimezone(UTC)
        if not result and stamp != start.astimezone(UTC):
            raise TimetableParseError("RRULE 与 DTSTART 的首次上课时间不一致，请修正后重新导出。")
        result.add(stamp)
        if len(result) > MAX_OCCURRENCES:
            raise TimetableParseError(f"展开课程超过 {MAX_OCCURRENCES} 次上限。")
        if count is not None and len(result) == count:
            break
    if not result or (count is not None and len(result) < count):
        raise TimetableParseError(f"重复规则无法在 {MAX_SPAN_DAYS} 天内完整展开，请检查规则或按学期导出。")
    return result

def _preflight(text: str) -> None:
    stack: list[str] = []
    events = lines = logical_length = 0
    for line in io.StringIO(text):
        line = line.rstrip("\r\n")
        lines += 1
        folded = line.startswith((" ", "\t"))
        logical_length = logical_length + len(line) if folded else len(line)
        if lines > 50_000 or logical_length > 32_768:
            raise TimetableParseError("ICS 内容过于复杂或字段过长，请精简后按学期导出。")
        if folded or not line:
            continue
        if ":" not in line:
            raise TimetableParseError("ICS 包含损坏的内容行，请重新导出。")
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            name = upper[6:]
            stack.append(name)
            events += name == "VEVENT"
            if len(stack) > 4 or events > MAX_SOURCE_EVENTS:
                raise TimetableParseError(f"ICS 嵌套过深或事件数量超过 {MAX_SOURCE_EVENTS} 条上限。")
        elif upper.startswith("END:"):
            if not stack or stack.pop() != upper[4:]:
                raise TimetableParseError("ICS 组件未正确闭合，请重新导出。")
    if stack:
        raise TimetableParseError("ICS 组件未正确闭合，请重新导出。")
    # icalendar normalizes TZID+Z into a local time, losing the contradictory Z.
    # Validate the original, unfolded value with its RFC content-line parser first.
    for line in Contentlines.from_ical(text):
        name = line.split(";", 1)[0].split(":", 1)[0].upper()
        if name not in {"DTSTART", "DTEND", "RECURRENCE-ID", "RDATE", "EXDATE"}:
            continue
        _, params, raw = line.parts()
        if "TZID" in params and any(value.upper().endswith("Z") for value in raw.split(",")):
            raise TimetableParseError("同一时间不能同时指定 TZID 和 UTC 标记 Z。")


def _parse_calendar(content: bytes) -> ParsedTimetable:
    inspect_calendar_payload(content)
    text = content.decode("utf-8-sig").strip()
    _preflight(text)
    calendar = Calendar.from_ical(text)
    if calendar.name != "VCALENDAR" or str(_single(calendar, "VERSION", required=True)) != "2.0":
        raise TimetableParseError("仅支持 VERSION:2.0 的 VCALENDAR 文件。")
    method = str(_single(calendar, "METHOD") or "PUBLISH").upper()
    if method != "PUBLISH":
        raise TimetableParseError("请导出完整课表，不支持会议邀请、回复或整份取消通知。")
    if any(component.errors for component in calendar.walk()):
        raise TimetableParseError("ICS 存在无法解码的字段，请修正后重新导出；不会跳过错误课程。")
    if any(child.name not in {"VEVENT", "VTIMEZONE"} for child in calendar.subcomponents):
        raise TimetableParseError("课表仅支持 VEVENT 课程事件，不支持任务或其他日历组件。")
    for zone in (child for child in calendar.subcomponents if child.name == "VTIMEZONE"):
        if any(child.name not in {"STANDARD", "DAYLIGHT"} or child.subcomponents for child in zone.subcomponents):
            raise TimetableParseError("VTIMEZONE 包含不支持的嵌套组件，不能在时区中嵌入课程。")
    events = [child for child in calendar.subcomponents if child.name == "VEVENT"]
    if not events:
        raise TimetableParseError("文件中没有 VEVENT 课程事件，请上传包含课程的 ICS 课表。")
    warnings: set[str] = set()
    masters: dict[str, Any] = {}
    overrides: dict[str, dict[datetime, Any]] = {}
    for event in events:
        if any(child.name != "VALARM" or child.subcomponents for child in event.subcomponents):
            raise TimetableParseError("VEVENT 包含不支持的嵌套组件。")
        if "EXRULE" in event:
            raise TimetableParseError("暂不支持 EXRULE，请改用 EXDATE 排除日期。")
        uid = _text(event, "UID", required=True, limit=512)
        status = str(_single(event, "STATUS") or "CONFIRMED").upper()
        if status not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
            raise TimetableParseError("课程 STATUS 状态无效。")
        recurrence = _single(event, "RECURRENCE-ID")
        if recurrence is None:
            if uid in masters:
                raise TimetableParseError("存在重复 UID 的主课程，无法确定应使用哪一份；请重新导出完整课表。")
            masters[uid] = event
        else:
            if recurrence.params.get("RANGE") is not None:
                raise TimetableParseError("暂不支持 RECURRENCE-ID;RANGE 的批量改期，请导出展开后的课表。")
            if any(key in event for key in ("RRULE", "RDATE", "EXDATE")):
                raise TimetableParseError("单次改期事件不能再次包含重复或排除规则。")
            stamp = _time(recurrence, "RECURRENCE-ID", warnings).astimezone(UTC)
            instances = overrides.setdefault(uid, {})
            if stamp in instances:
                raise TimetableParseError("同一课程的同一次上课存在多个改期或取消记录，请合并后重新导出。")
            instances[stamp] = event
    if set(overrides) - set(masters):
        raise TimetableParseError("单次改期或取消记录缺少对应 UID 的主课程，请导出完整课表。")
    courses = {uid: _course(event, warnings) for uid, event in masters.items()}
    rdates = {uid: _dates(event, "RDATE", warnings) for uid, event in masters.items()}
    exdates = {uid: _dates(event, "EXDATE", warnings) for uid, event in masters.items()}
    explicit_starts = [course.start.astimezone(UTC) for course in courses.values()]
    explicit_starts.extend(stamp for dates in rdates.values() for stamp in dates)
    for instances in overrides.values():
        for event in instances.values():
            value = _single(event, "DTSTART")
            if value is not None:
                explicit_starts.append(_time(value, "DTSTART", warnings).astimezone(UTC))
    earliest = min(explicit_starts)
    deadline = earliest + timedelta(days=MAX_SPAN_DAYS)
    if max(explicit_starts) > deadline:
        raise TimetableParseError(f"课表日期范围超过 {MAX_SPAN_DAYS} 天上限，请按学期导出。")
    occurrences: list[CourseOccurrence] = []
    expanded_count = 0
    for uid, base in courses.items():
        event = masters[uid]
        starts = _expand(event, base.start, deadline) | rdates[uid]
        expanded_count += len(starts)
        if expanded_count > MAX_OCCURRENCES:
            raise TimetableParseError(f"整份课表展开超过 {MAX_OCCURRENCES} 次上限，请按学期导出。")
        changes = overrides.get(uid, {})
        if set(changes) - starts:
            raise TimetableParseError("RECURRENCE-ID 未匹配任何原始上课时间，请导出完整课表。")
        if set(changes) & exdates[uid]:
            raise TimetableParseError("同一次课程同时出现在 EXDATE 和改期/取消记录中，请消除冲突后重新导出。")
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            if changes:
                raise TimetableParseError("已整门取消的课程仍包含单次改期记录，请消除冲突后重新导出。")
            continue
        for stamp in starts - exdates[uid]:
            change = changes.get(stamp)
            if change is not None and str(change.get("STATUS", "")).upper() == "CANCELLED":
                continue
            course = _course(change, warnings, base) if change is not None else base
            start = course.start.astimezone(UTC) if change is not None else stamp
            end = start + course.duration
            if start < earliest or end > deadline:
                raise TimetableParseError(f"课表日期范围超过 {MAX_SPAN_DAYS} 天上限，请按学期导出。")
            occurrences.append(CourseOccurrence(
                uid, stamp, course.name, start.astimezone(SHANGHAI), end.astimezone(SHANGHAI), course.location,
            ))
    if not occurrences:
        raise TimetableParseError("应用排除和取消记录后没有可用课程，请检查课表。")
    occurrences.sort(key=lambda item: (item.starts_at, item.ends_at, item.name, item.uid, item.recurrence_id))
    return ParsedTimetable(len(events), len(masters), tuple(occurrences), tuple(sorted(warnings)))


def parse_calendar(content: bytes) -> ParsedTimetable:
    """Synchronous parser for tests/tools; the bot uses parse_calendar_isolated()."""
    try:
        return _parse_calendar(content)
    except TimetableParseError:
        raise
    except TimetableFileError as exc:
        raise TimetableParseError(str(exc)) from None
    except Exception:
        # Library exception messages can contain student names, URLs and whole ICS lines.
        raise TimetableParseError("ICS 格式、日期或重复规则无效，无法完整解析；请重新导出课表。") from None


def _to_wire(result: ParsedTimetable) -> dict[str, Any]:
    return {
        "source_event_count": result.source_event_count, "series_count": result.series_count,
        "warnings": result.warnings,
        "occurrences": [
            [item.uid, item.recurrence_id.isoformat(), item.name,
             item.starts_at.isoformat(), item.ends_at.isoformat(), item.location]
            for item in result.occurrences
        ],
    }


def _from_wire(data: dict[str, Any]) -> ParsedTimetable:
    return ParsedTimetable(data["source_event_count"], data["series_count"], tuple(
        CourseOccurrence(uid, datetime.fromisoformat(stamp).astimezone(UTC), name,
                         datetime.fromisoformat(start).astimezone(SHANGHAI),
                         datetime.fromisoformat(end).astimezone(SHANGHAI), location)
        for uid, stamp, name, start, end, location in data["occurrences"]
    ), tuple(data["warnings"]))


async def parse_calendar_isolated(content: bytes, *, timeout_seconds: float = PARSE_TIMEOUT_SECONDS) -> ParsedTimetable:
    """Run a bounded worker with pipe-only input; terminate it on timeout/cancellation."""
    if timeout_seconds <= 0:
        raise ValueError("Parser timeout must be positive")
    if len(content) > MAX_ICS_BYTES:
        raise TimetableParseError("ICS 文件超过 10 MiB 上限。")
    kwargs: dict[str, Any] = {"cwd": str(Path(__file__).resolve().parents[1])}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: never flash a console.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "plugins.timetable_ics", "--worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, **kwargs,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(content), timeout_seconds)
    except (Exception, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, asyncio.TimeoutError):
            raise TimetableParseError("ICS 解析超时，已终止解析；请精简课表或按学期导出后重试。") from None
        raise TimetableParseError("ICS 解析进程通信中断，已终止解析；请稍后重试。") from None
    try:
        if process.returncode != 0 or not stdout or len(stdout) > MAX_RESULT_BYTES:
            raise ValueError("Invalid worker result")
        data = json.loads(stdout)
        if "error" in data:
            raise TimetableParseError(data["error"])
        return _from_wire(data)
    except TimetableParseError:
        raise
    except Exception:
        raise TimetableParseError("ICS 解析进程未能完成，请精简课表后重试。") from None


def _worker_main() -> None:
    # Linux containers can also cap parser address space; Windows relies on input,
    # component, output and time limits. Neither platform writes the ICS to disk.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ImportError, OSError, ValueError):
        pass
    try:
        data = _to_wire(parse_calendar(sys.stdin.buffer.read(MAX_ICS_BYTES + 1)))
        output = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(output) > MAX_RESULT_BYTES:
            raise TimetableParseError("解析结果过大，请精简字段并按学期导出。")
    except TimetableParseError as exc:
        output = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
    except Exception:
        output = json.dumps({"error": "解析资源不足或结果无效，请精简课表后重试。"}, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    _worker_main()
