"""SQLite persistence for normalized courses; no raw calendars or network access.

All SQL runs in one bounded worker, never on the bot's event loop. Each operation
owns its connection, and a replacement publishes metadata and courses together.
"""

import asyncio
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar
from uuid import uuid4

from .timetable_ics import CourseOccurrence, ParsedTimetable, SHANGHAI


LOGGER = logging.getLogger("qq-bot")
SCHEMA_VERSION = 1
MAX_PENDING_DB_JOBS = 32
MAX_QUERY_ROWS = 1000
# Legacy timestamps have second precision; fixed tie-breaking never mixes versions.
SHARED_IMPORT_ORDER = "updated_at DESC, imported_at DESC, group_id DESC"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
Result = TypeVar("Result")


class TimetableStoreError(RuntimeError):
    """Only user-safe messages, without SQL, paths or calendar contents."""


class TimetableConflictError(TimetableStoreError):
    """The import changed after this upload session was started."""


@dataclass(frozen=True)
class StoredTimetable:
    group_id: str
    user_id: str
    revision: str
    filename: str
    sha256: str
    source_event_count: int
    series_count: int
    occurrence_count: int
    imported_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MemberCourses:
    user_id: str
    courses: tuple[CourseOccurrence, ...]


def _seconds(when: datetime) -> int:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    delta = when.astimezone(timezone.utc) - EPOCH
    return delta.days * 86400 + delta.seconds


def _datetime(seconds: int) -> datetime:
    # Unlike fromtimestamp(), this also works for pre-1970 dates on Windows.
    return (EPOCH + timedelta(seconds=seconds)).astimezone(SHANGHAI)


def _record(row: sqlite3.Row) -> StoredTimetable:
    return StoredTimetable(
        row["group_id"], row["user_id"], row["revision"], row["filename"], row["sha256"],
        row["source_event_count"], row["series_count"], row["occurrence_count"],
        _datetime(row["imported_at"]), _datetime(row["updated_at"]),
    )


def _course(row: sqlite3.Row) -> CourseOccurrence:
    return CourseOccurrence(
        row["uid"], _datetime(row["recurrence_id"]).astimezone(timezone.utc), row["name"],
        _datetime(row["starts_at"]), _datetime(row["ends_at"]), row["location"],
    )


class TimetableStore:
    def __init__(
        self, path: str | Path | None = None, *, timeout: float = 3.0,
        group_isolation_enabled: bool = True,
    ) -> None:
        default = Path(__file__).resolve().parents[1] / "data" / "timetable.sqlite3"
        self.path = Path(path if path is not None else os.getenv("TIMETABLE_DB_PATH", str(default)))
        self.timeout = timeout
        self.group_isolation_enabled = group_isolation_enabled
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="timetable-db")
        self._pending = 0
        self._closed = False
        self._initialized = False

    def close(self) -> None:
        # Submitted transactions finish atomically; no new jobs are accepted.
        self._closed = True
        self._executor.shutdown(wait=False)

    async def _run(self, operation: Callable[..., Result], *args: object) -> Result:
        if self._closed:
            raise TimetableStoreError("课表数据库已关闭，请稍后重试。")
        if self._pending >= MAX_PENDING_DB_JOBS:
            raise TimetableStoreError("课表数据库繁忙，请稍后重试。")
        self._pending += 1
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, operation, *args)
        except Exception:
            self._pending -= 1
            raise

        def finished(done: asyncio.Future) -> None:
            self._pending -= 1
            # Retrieve errors even if the requesting WebSocket task was cancelled.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        try:
            # Cancelling an event cannot interrupt a half-written SQLite transaction.
            return await asyncio.shield(future)
        except TimetableStoreError:
            raise
        except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
            LOGGER.warning("Timetable database operation failed (%s)", type(exc).__name__)
            raise TimetableStoreError("课表数据库暂时不可用，请稍后重试。") from None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            if not self._initialized:
                self._initialize(connection)
                self._initialized = True
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise TimetableStoreError("课表数据库版本不兼容，请联系管理员；现有数据未被修改。")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS timetable_imports (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    source_event_count INTEGER NOT NULL CHECK (source_event_count > 0),
                    series_count INTEGER NOT NULL CHECK (series_count > 0),
                    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                    imported_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS timetable_occurrences (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    recurrence_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
                    location TEXT NOT NULL,
                    PRIMARY KEY (group_id, user_id, uid, recurrence_id),
                    FOREIGN KEY (group_id, user_id)
                        REFERENCES timetable_imports (group_id, user_id) ON DELETE CASCADE
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS timetable_group_time
                ON timetable_occurrences (group_id, starts_at, ends_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS timetable_member_time
                ON timetable_occurrences (group_id, user_id, starts_at, ends_at)
            """)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    async def get_import(self, key: tuple[str, str]) -> StoredTimetable | None:
        return await self._run(self._get_import, key)

    def _get_import(self, key: tuple[str, str]) -> StoredTimetable | None:
        with self._connection() as connection:
            row = self._select_import(connection, key)
            return _record(row) if row is not None else None

    def _select_import(self, connection: sqlite3.Connection, key: tuple[str, str]) -> sqlite3.Row | None:
        if self.group_isolation_enabled:
            return connection.execute(
                "SELECT * FROM timetable_imports WHERE group_id = ? AND user_id = ?", key,
            ).fetchone()
        # Reuse existing records without migrating, copying, or deleting group data.
        return connection.execute(
            "SELECT * FROM timetable_imports WHERE user_id = ? "
            f"ORDER BY {SHARED_IMPORT_ORDER} LIMIT 1", (key[1],),
        ).fetchone()

    async def save(
        self, key: tuple[str, str], filename: str, sha256: str, parsed: ParsedTimetable,
        *, expected_revision: str | None = None,
    ) -> StoredTimetable:
        """Create in the sending group, or replace the active scoped revision.

        Shared updates retain the selected record's original group. Historical
        records from other groups remain available when isolation is re-enabled.
        """
        return await self._run(self._save, key, filename, sha256, parsed, expected_revision)

    def _save(
        self, key: tuple[str, str], filename: str, sha256: str, parsed: ParsedTimetable,
        expected_revision: str | None,
    ) -> StoredTimetable:
        rows = [(course.uid, _seconds(course.recurrence_id), course.name,
                 _seconds(course.starts_at), _seconds(course.ends_at), course.location)
                for course in parsed.occurrences]
        now = _seconds(datetime.now(timezone.utc))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Resolve again under the write lock: even uploads in different
                # groups must compare against the same current shared revision.
                old = self._select_import(connection, key)
                if expected_revision is None and old is not None:
                    raise TimetableConflictError("你已导入课表，未覆盖现有数据；请 @bot /更新课表 重新开始。")
                if expected_revision is not None and old is None:
                    raise TimetableConflictError("你尚未导入课表；请 @bot /导入课表 后上传 .ics 文件，再 @bot /已导入。")
                if old is not None and old["revision"] != expected_revision:
                    raise TimetableConflictError("课表已发生变更，本次未覆盖；请 @bot /更新课表 重新开始。")
                stored_key = (old["group_id"], old["user_id"]) if old is not None else key
                revision = uuid4().hex
                created = old["imported_at"] if old is not None else now
                # A clock correction must not make another legacy record supersede
                # a just-updated shared timetable.
                now = max(now, old["updated_at"]) if old is not None else now
                connection.execute("""
                    INSERT INTO timetable_imports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        revision = excluded.revision, filename = excluded.filename,
                        sha256 = excluded.sha256, source_event_count = excluded.source_event_count,
                        series_count = excluded.series_count, occurrence_count = excluded.occurrence_count,
                        updated_at = excluded.updated_at
                """, (*stored_key, revision, filename, sha256, parsed.source_event_count,
                      parsed.series_count, len(rows), created, now))
                connection.execute(
                    "DELETE FROM timetable_occurrences WHERE group_id = ? AND user_id = ?", stored_key,
                )
                connection.executemany("""
                    INSERT INTO timetable_occurrences
                    (group_id, user_id, uid, recurrence_id, name, starts_at, ends_at, location)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ((*stored_key, *row) for row in rows))
                connection.commit()
                return StoredTimetable(*stored_key, revision, filename, sha256, parsed.source_event_count,
                                       parsed.series_count, len(rows), _datetime(created), _datetime(now))
            except BaseException:
                connection.rollback()
                raise

    async def current_courses(
        self, group_id: str, now: datetime, *, user_ids: Iterable[str] | None = None,
    ) -> tuple[MemberCourses, ...]:
        allowed = tuple(user_ids) if user_ids is not None else None
        return await self._run(self._current_courses, group_id, _seconds(now), allowed)

    def _current_courses(
        self, group_id: str, now: int, user_ids: tuple[str, ...] | None,
    ) -> tuple[MemberCourses, ...]:
        if user_ids == ():
            return ()
        if not self.group_isolation_enabled and user_ids is None:
            # A shared query without an allowlist could leak users from other groups.
            raise TimetableStoreError("共享课表查询需要当前群成员名单，请稍后重试。")
        with self._connection() as connection:
            member_filter = ""
            if user_ids is not None:
                # Connection-local only: no schema migration, persisted names or deletions.
                # One binding per insert also works for groups above SQLite's variable limit.
                connection.execute("CREATE TEMP TABLE current_members (user_id TEXT PRIMARY KEY) WITHOUT ROWID")
                connection.executemany(
                    "INSERT OR IGNORE INTO current_members (user_id) VALUES (?)", ((user,) for user in user_ids),
                )
                member_filter = " AND i.user_id IN (SELECT user_id FROM current_members)"
            source = "timetable_imports AS i"
            scope_filter = "i.group_id = ?" + member_filter
            scope_args = (group_id,)
            if not self.group_isolation_enabled:
                # Filter to this group's members BEFORE choosing one full version
                # per QQ. Ranking is bounded by stored imports, not an N+1 query.
                source = f"""(
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY user_id ORDER BY {SHARED_IMPORT_ORDER}
                    ) AS shared_rank
                    FROM timetable_imports
                    WHERE user_id IN (SELECT user_id FROM current_members)
                ) AS i"""
                scope_filter = "i.shared_rank = 1"
                scope_args = ()
            rows = connection.execute(f"""
                SELECT i.user_id, c.uid, c.recurrence_id, c.name, c.starts_at, c.ends_at, c.location
                FROM {source}
                LEFT JOIN timetable_occurrences AS c ON
                    c.group_id = i.group_id AND c.user_id = i.user_id
                    AND c.starts_at <= ? AND c.ends_at > ?
                WHERE {scope_filter}
                ORDER BY length(i.user_id), i.user_id, c.starts_at, c.ends_at, c.uid, c.recurrence_id
                LIMIT ?
            """, (now, now, *scope_args, MAX_QUERY_ROWS + 1)).fetchall()
            self._check_query_limit(rows)
            members: dict[str, list[CourseOccurrence]] = {}
            for row in rows:
                courses = members.setdefault(row["user_id"], [])
                if row["uid"] is not None:
                    courses.append(_course(row))
            return tuple(MemberCourses(user, tuple(courses)) for user, courses in members.items())

    async def today_courses(self, key: tuple[str, str], now: datetime) -> tuple[CourseOccurrence, ...] | None:
        day = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._run(self._today_courses, key, _seconds(day), _seconds(day + timedelta(days=1)))

    def _today_courses(self, key: tuple[str, str], start: int, end: int) -> tuple[CourseOccurrence, ...] | None:
        with self._connection() as connection:
            # The existence check and the course query see the same committed version.
            connection.execute("BEGIN")
            selected = self._select_import(connection, key)
            if selected is None:
                return None
            stored_key = (selected["group_id"], selected["user_id"])
            rows = connection.execute("""
                SELECT * FROM timetable_occurrences
                WHERE group_id = ? AND user_id = ? AND starts_at < ? AND ends_at > ?
                ORDER BY starts_at, ends_at, uid, recurrence_id LIMIT ?
            """, (*stored_key, end, start, MAX_QUERY_ROWS + 1)).fetchall()
            self._check_query_limit(rows)
            return tuple(_course(row) for row in rows)

    @staticmethod
    def _check_query_limit(rows: list[sqlite3.Row]) -> None:
        if len(rows) > MAX_QUERY_ROWS:
            raise TimetableStoreError("课程查询结果过多，超过单次查询上限；请缩小课表内容或使用 /今日课程 查看本人课表。")
