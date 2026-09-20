"""Exercise bounded admission and real thread lifetimes without network access."""
import asyncio
import threading
import unittest

from plugins.timetable_avatar import AvatarCache
from plugins.timetable_images import TimetableImageBusy, TimetableImageService
from plugins.timetable_render import TimetableRenderError


class ImageServiceTest(unittest.IsolatedAsyncioTestCase):
    def service(self, **kwargs):
        service = TimetableImageService(avatars=AvatarCache(fetch=lambda _: None), **kwargs)
        self.addCleanup(service.close)
        return service

    async def test_duplicate_requests_and_admission_cap_are_bounded(self):
        service = self.service(workers=1, max_requests=1)
        async with service.request("first"):
            for key in ("first", "other"):
                with self.subTest(key=key), self.assertRaises(TimetableImageBusy):
                    async with service.request(key):
                        self.fail("Unexpected admission")
        self.assertEqual(service._requests, set())
        async with service.request("first"):
            pass

    async def test_queue_timeout_removes_only_its_own_reservation(self):
        service = self.service(workers=1, max_requests=2, queue_timeout=.03)
        async with service.request("active"):
            with self.assertRaises(TimetableImageBusy):
                async with service.request("queued"):
                    self.fail("Unexpected admission")
            self.assertEqual(service._requests, {"active"})
            self.assertEqual(service._request_slots._value, 0)
        self.assertEqual(service._request_slots._value, 1)

    async def test_cancelled_queue_does_not_leak_request_slot(self):
        service = self.service(workers=1, max_requests=2)
        async def queued():
            async with service.request("queued"):
                pass
        async with service.request("active"):
            task = asyncio.create_task(queued())
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(service._requests, {"active"})
        self.assertEqual(service._request_slots._value, 1)

    async def test_request_exception_releases_reservation(self):
        service = self.service()
        with self.assertRaises(ValueError):
            async with service.request("request"):
                raise ValueError("synthetic")
        self.assertEqual(service._requests, set())

    def blocked_job(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block():
            started.set()
            release.wait(3)
            return "result"
        return block, started, release

    async def test_timeout_keeps_worker_slot_until_thread_really_finishes(self):
        service = self.service(workers=1, queue_timeout=.03, work_timeout=.03)
        block, started, release = self.blocked_job()
        with self.assertRaisesRegex(TimetableRenderError, "绘图超时"):
            await service.work(block)
        self.assertTrue(started.is_set())
        self.assertEqual(len(service._jobs), 1)
        self.assertEqual(service._worker_slots._value, 0)
        with self.assertRaises(TimetableImageBusy):
            await service.work(lambda: "must not start")
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(service._jobs)), 2)
        service.work_timeout = 1
        self.assertEqual(await service.work(lambda: "ready"), "ready")

    async def test_cancelled_await_does_not_free_running_thread_early(self):
        service = self.service(workers=1)
        block, started, release = self.blocked_job()
        task = asyncio.create_task(service.work(block))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(service._worker_slots._value, 0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(service._jobs)), 2)
        self.assertEqual(service._worker_slots._value, 1)

    async def test_work_runs_off_event_loop_and_errors_release_slot(self):
        service = self.service(workers=1)
        caller = threading.get_ident()
        self.assertNotEqual(await service.work(threading.get_ident), caller)
        def fail():
            raise ValueError("synthetic")
        with self.assertRaises(ValueError):
            await service.work(fail)
        self.assertEqual(service._worker_slots._value, 1)
        self.assertEqual(service._jobs, set())

    async def test_blocked_worker_does_not_block_other_async_commands(self):
        service = self.service()
        block, started, release = self.blocked_job()
        task = asyncio.create_task(service.work(block))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        await asyncio.wait_for(asyncio.sleep(.01), .5)
        self.assertFalse(task.done())
        release.set()
        self.assertEqual(await task, "result")

    async def test_close_discards_result_of_running_work_and_rejects_new_work(self):
        service = self.service(workers=1)
        block, started, release = self.blocked_job()
        task = asyncio.create_task(service.work(block))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        service.close()
        release.set()
        with self.assertRaises(TimetableRenderError):
            await task
        with self.assertRaises(TimetableRenderError):
            await service.work(lambda: "late")
        with self.assertRaises(TimetableImageBusy):
            async with service.request("late"):
                pass

    async def test_invalid_limits_are_rejected(self):
        for kwargs in ({"workers": 0}, {"max_requests": 0}, {"queue_timeout": 0}, {"work_timeout": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service(**kwargs)


if __name__ == "__main__":
    unittest.main()