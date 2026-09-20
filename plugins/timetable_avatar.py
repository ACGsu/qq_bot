"""Fixed-host QQ avatar loading with bounded, expiring, memory-only caching."""
from __future__ import annotations

import asyncio
import http.client
import io
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Callable, Iterable

MAX_DOWNLOAD_BYTES = 512 * 1024
MAX_SOURCE_PIXELS = 2_000_000
MAX_AVATAR_BYTES = 96 * 1024
AVATAR_SIZE = 160
AVATAR_HOST = "q1.qlogo.cn"


def normalized_qq(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) or int(value) <= 0:
        raise ValueError("Invalid avatar identity")
    return str(int(value))


def normalize_avatar(content: bytes) -> bytes:
    from PIL import Image, ImageOps
    if not content or len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Avatar size limit")
    with io.BytesIO(content) as source_bytes, Image.open(source_bytes, formats=("PNG", "JPEG", "WEBP")) as source:
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise ValueError("Avatar pixel limit")
        with source.convert("RGB") as rgb, ImageOps.fit(rgb, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS) as image:
            with io.BytesIO() as output:
                image.save(output, format="PNG", compress_level=3)
                result = output.getvalue()
    if len(result) > MAX_AVATAR_BYTES:
        raise ValueError("Normalized avatar size limit")
    return result


def fetch_avatar(user_id: str) -> bytes | None:
    """No user URL, redirects, proxies, file writes, cookies, or ICS metadata."""
    qq = normalized_qq(user_id)
    connection = http.client.HTTPSConnection(AVATAR_HOST, timeout=2)
    deadline = time.monotonic() + 3
    try:
        connection.request("GET", f"/g?b=qq&nk={qq}&s=100", headers={"User-Agent": "QQBot-Timetable/1"})
        with connection.getresponse() as response:
            if response.status != 200:  # Do not follow redirects to arbitrary hosts.
                return None
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0].lower().strip()
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                return None
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_DOWNLOAD_BYTES):
                return None
            with io.BytesIO() as output:
                while output.tell() <= MAX_DOWNLOAD_BYTES:
                    if time.monotonic() > deadline:
                        return None
                    chunk = response.read(min(16384, MAX_DOWNLOAD_BYTES + 1 - output.tell()))
                    if not chunk:
                        return normalize_avatar(output.getvalue())
                    output.write(chunk)
                return None
    except Exception:
        # Remote image content and errors must never leak into replies/logs.
        return None
    finally:
        connection.close()


def _consume(future):
    if not future.cancelled():
        future.exception()


class AvatarCache:
    def __init__(self, *, fetch: Callable[[str], bytes | None] | None = None,
                 capacity: int = 128, ttl: float = 6 * 3600, negative_ttl: float = 60,
                 wait_seconds: float = 1.2, max_pending: int = 8, workers: int = 4):
        if min(capacity, ttl, negative_ttl, wait_seconds, max_pending, workers) <= 0:
            raise ValueError("Invalid avatar limits")
        self.fetch = fetch or fetch_avatar
        self.capacity, self.ttl, self.negative_ttl = capacity, ttl, negative_ttl
        self.wait_seconds, self.max_pending = wait_seconds, max_pending
        self._cache: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()
        self._tasks: dict[str, asyncio.Task] = {}
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="timetable-avatar")
        self._closed = False

    def _entry(self, qq: str):
        entry = self._cache.get(qq)
        if entry is not None and entry[0] <= time.monotonic():
            self._cache.pop(qq, None)
            entry = None
        if entry is not None:
            self._cache.move_to_end(qq)
        return entry

    async def _load(self, qq: str):
        result = None
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, self.fetch, qq)
            future.add_done_callback(_consume)
            result = await asyncio.shield(future)
            if not isinstance(result, bytes) or not result or len(result) > MAX_AVATAR_BYTES:
                result = None
        except Exception:
            pass
        if not self._closed:
            self._cache[qq] = (time.monotonic() + (self.ttl if result else self.negative_ttl), result)
            self._cache.move_to_end(qq)
            while len(self._cache) > self.capacity:
                self._cache.popitem(last=False)

    def _done(self, qq, task):
        if self._tasks.get(qq) is task:
            self._tasks.pop(qq, None)
        _consume(task)

    async def get_many(self, user_ids: Iterable[str]) -> dict[str, bytes | None]:
        # Avoid a huge dictionary even if the group itself has many members.
        ids = tuple(dict.fromkeys(normalized_qq(q) for q in islice(user_ids, 512)))
        if self._closed:
            return {}
        waiting = []
        for qq in ids:
            if self._entry(qq) is not None:
                continue
            task = self._tasks.get(qq)
            if task is None and len(self._tasks) < self.max_pending:
                task = asyncio.create_task(self._load(qq))
                self._tasks[qq] = task
                task.add_done_callback(lambda done, q=qq: self._done(q, done))
            if task is not None:
                waiting.append(task)
        if waiting:
            # wait() does not cancel shared downloads when a query's wait budget ends.
            await asyncio.wait(waiting, timeout=self.wait_seconds)
        return {qq: entry[1] if (entry := self._entry(qq)) is not None else None for qq in ids}

    def close(self):
        self._closed = True
        self._cache.clear()
        for task in tuple(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
