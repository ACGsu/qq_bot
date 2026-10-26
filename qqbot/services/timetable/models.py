"""Framework-neutral request models used by timetable orchestration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TimetableCommand:
    text: str
    mentions: list[str] | tuple[str, ...]
