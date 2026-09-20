"""Bounded asynchronous admission and worker lifetime for in-memory rendering."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from .timetable_avatar import AvatarCache
from .timetable_render import RenderLimits, TimetableRenderError, build_plan, render_page


class TimetableImageBusy(TimetableRenderError):
    pass


class TimetableImageService:
    def __init__(self, *, limits: RenderLimits = RenderLimits(), avatars: AvatarCache | None = None,
                 workers: int = 2, max_requests: int = 8, queue_timeout: float = 2,
                 work_timeout: float = 4):
        if min(workers, max_requests, queue_timeout, work_timeout) <= 0:
            raise ValueError("Invalid image service limits")
        self.limits = limits
        self.avatars = avatars if avatars is not None else AvatarCache()
        self.max_requests, self.queue_timeout, self.work_timeout = max_requests, queue_timeout, work_timeout
        self._requests = set()
        self._request_slots = asyncio.Semaphore(workers)
        self._worker_slots = asyncio.Semaphore(workers)
        self._jobs = set()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="timetable-render")
        self._closed = False

    @asynccontextmanager
    async def request(self, key):
        if self._closed or key in self._requests or len(self._requests) >= self.max_requests:
            raise TimetableImageBusy("图片服务繁忙")
        self._requests.add(key)
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._request_slots.acquire(), self.queue_timeout)
                acquired = True
            except asyncio.TimeoutError:
                raise TimetableImageBusy("图片请求排队超时") from None
            if self._closed:
                raise TimetableImageBusy("图片服务已关闭")
            yield self
        finally:
            if acquired:
                self._request_slots.release()
            self._requests.discard(key)

    def _finished(self, future):
        self._jobs.discard(future)
        self._worker_slots.release()
        if not future.cancelled():
            future.exception()  # Also consume failures from timed-out/abandoned work.

    async def work(self, function, *args):
        if self._closed:
            raise TimetableRenderError("图片服务已关闭")
        try:
            await asyncio.wait_for(self._worker_slots.acquire(), self.queue_timeout)
        except asyncio.TimeoutError:
            raise TimetableImageBusy("绘图工作线程繁忙") from None
        if self._closed:
            self._worker_slots.release()
            raise TimetableRenderError("图片服务已关闭")
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        except Exception:
            self._worker_slots.release()
            raise
        self._jobs.add(future)
        future.add_done_callback(self._finished)
        try:
            # Cancellation of an await does not kill a Pillow thread. Its slot is
            # released ONLY by _finished(), not by timeout or request cancellation.
            result = await asyncio.wait_for(asyncio.shield(future), self.work_timeout)
        except asyncio.TimeoutError:
            raise TimetableRenderError("绘图超时") from None
        if self._closed:
            raise TimetableRenderError("图片服务已关闭")
        return result

    async def plan(self, view):
        return await self.work(build_plan, view, self.limits)

    async def page(self, plan, index, avatars):
        return await self.work(render_page, plan, index, avatars)

    def close(self):
        self._closed = True
        self.avatars.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
