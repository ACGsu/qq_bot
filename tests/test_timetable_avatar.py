"""Synthetic avatar bytes and mocked HTTPS only; never contact real QQ users."""
import asyncio
import io
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

from plugins.timetable_avatar import (
    AVATAR_HOST, AVATAR_SIZE, MAX_AVATAR_BYTES, MAX_DOWNLOAD_BYTES,
    AvatarCache, fetch_avatar, normalize_avatar, normalized_qq,
)


def png(size=(80, 80), color="teal"):
    with Image.new("RGB", size, color) as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


class AvatarDecodeTest(unittest.TestCase):
    def response(self, content, *, status=200, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.getheader.side_effect = (headers or {"Content-Type": "image/png"}).get
        response.read.side_effect = io.BytesIO(content).read
        return response

    def test_numeric_identity_only(self):
        self.assertEqual(normalized_qq("00111"), "111")
        for value in ("0", "-1", "1&url=file:///etc/passwd", "１２３", "1/2", "9" * 21, 111):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalized_qq(value)

    def test_normalizes_in_memory_to_small_rgb_png(self):
        result = normalize_avatar(png((240, 120)))
        self.assertLess(len(result), MAX_AVATAR_BYTES)
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (AVATAR_SIZE, AVATAR_SIZE))
            self.assertEqual(image.mode, "RGB")

    def test_rejects_empty_oversized_invalid_or_disallowed_formats(self):
        with Image.new("RGB", (10, 10)) as image, io.BytesIO() as buffer:
            image.save(buffer, format="BMP")
            bmp = buffer.getvalue()
        for content in (b"", b"x" * (MAX_DOWNLOAD_BYTES + 1), b"<html>not an avatar", bmp):
            with self.subTest(size=len(content)), self.assertRaises((ValueError, OSError)):
                normalize_avatar(content)

    def test_pixel_and_encoded_size_caps(self):
        with patch("plugins.timetable_avatar.MAX_SOURCE_PIXELS", 50), self.assertRaises(ValueError):
            normalize_avatar(png())
        with patch("plugins.timetable_avatar.MAX_AVATAR_BYTES", 10), self.assertRaises(ValueError):
            normalize_avatar(png())

    def test_fixed_https_host_no_untrusted_url_and_connection_closed(self):
        connection = Mock()
        connection.getresponse.return_value = self.response(png())
        with patch("plugins.timetable_avatar.http.client.HTTPSConnection", return_value=connection) as factory:
            self.assertTrue(fetch_avatar("111").startswith(b"\x89PNG"))
        factory.assert_called_once_with(AVATAR_HOST, timeout=2)
        args = connection.request.call_args.args
        self.assertEqual(args, ("GET", "/g?b=qq&nk=111&s=100"))
        connection.close.assert_called_once()

    def test_redirect_error_mime_length_and_corrupt_content_fall_back(self):
        cases = [
            (302, {"Content-Type": "image/png", "Location": "http://127.0.0.1/"}, png()),
            (404, {"Content-Type": "image/png"}, png()),
            (200, {"Content-Type": "text/html"}, png()),
            (200, {"Content-Type": "image/png", "Content-Length": str(MAX_DOWNLOAD_BYTES + 1)}, png()),
            (200, {"Content-Type": "image/png", "Content-Length": "-1"}, png()),
            (200, {"Content-Type": "image/png"}, b"bad"),
            (200, {"Content-Type": "image/png"}, b"x" * (MAX_DOWNLOAD_BYTES + 1)),
        ]
        for status, headers, content in cases:
            connection = Mock()
            connection.getresponse.return_value = self.response(content, status=status, headers=headers)
            with self.subTest(status=status, headers=headers), patch(
                "plugins.timetable_avatar.http.client.HTTPSConnection", return_value=connection,
            ):
                self.assertIsNone(fetch_avatar("111"))
                connection.request.assert_called_once()
                connection.close.assert_called_once()

    def test_network_timeout_and_total_deadline_are_safe(self):
        connection = Mock()
        connection.request.side_effect = TimeoutError("private remote details")
        with patch("plugins.timetable_avatar.http.client.HTTPSConnection", return_value=connection):
            self.assertIsNone(fetch_avatar("111"))
        connection.close.assert_called_once()
        connection = Mock()
        connection.getresponse.return_value = self.response(png())
        with patch("plugins.timetable_avatar.http.client.HTTPSConnection", return_value=connection), patch(
            "plugins.timetable_avatar.time.monotonic", side_effect=[0, 4],
        ):
            self.assertIsNone(fetch_avatar("111"))
            connection.getresponse.return_value.read.assert_not_called()


class AvatarCacheTest(unittest.IsolatedAsyncioTestCase):
    def cache(self, **kwargs):
        cache = AvatarCache(**kwargs)
        self.addCleanup(cache.close)
        return cache

    async def test_cache_hit_and_duplicate_ids_do_not_repeat_fetch(self):
        fetch = Mock(return_value=png())
        cache = self.cache(fetch=fetch)
        first = await cache.get_many(["111", "111", "00111"])
        self.assertEqual(first, await cache.get_many(["111"]))
        fetch.assert_called_once_with("111")

    async def test_lru_capacity_and_expiry(self):
        fetch = Mock(return_value=png())
        cache = self.cache(fetch=fetch, capacity=2)
        for qq in ("111", "222", "111", "333"):
            await cache.get_many([qq])
        self.assertEqual(list(cache._cache), ["111", "333"])
        cache._cache["111"] = (0, png())
        await cache.get_many(["111"])
        self.assertEqual(fetch.call_count, 4)

    async def test_negative_cache_has_shorter_ttl_and_recovers(self):
        fetch = Mock(side_effect=[OSError("private"), png()])
        cache = self.cache(fetch=fetch)
        self.assertEqual(await cache.get_many(["111"]), {"111": None})
        await cache.get_many(["111"])
        fetch.assert_called_once()
        self.assertLess(cache._cache["111"][0] - asyncio.get_running_loop().time(), 61)
        cache._cache["111"] = (0, None)
        self.assertIsNotNone((await cache.get_many(["111"]))["111"])

    async def test_slow_shared_fetch_is_bounded_and_not_cancelled_by_one_query(self):
        started, release = threading.Event(), threading.Event()
        def fetch(qq):
            started.set()
            release.wait(2)
            return png()
        cache = self.cache(fetch=Mock(side_effect=fetch), wait_seconds=2, max_pending=1, workers=1)
        self.addCleanup(release.set)
        query = asyncio.create_task(cache.get_many(["111"]))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        query.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await query
        cache.wait_seconds = .03
        self.assertEqual(await cache.get_many(["111", "222"]), {"111": None, "222": None})
        self.assertEqual(len(cache._tasks), 1)
        cache.fetch.assert_called_once()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(cache._tasks.values())), 2)
        self.assertIsNotNone((await cache.get_many(["111"]))["111"])

    async def test_entry_size_cap_and_closed_cache(self):
        cache = self.cache(fetch=Mock(return_value=b"x" * (MAX_AVATAR_BYTES + 1)))
        self.assertEqual(await cache.get_many(["111"]), {"111": None})
        cache.close()
        self.assertEqual(cache._cache, {})
        self.assertEqual(await cache.get_many(["222"]), {})
        cache.fetch.assert_called_once()

    async def test_identity_iteration_is_bounded(self):
        cache = self.cache(fetch=lambda _: None)
        def ids():
            yield from (str(i) for i in range(1, 513))
            raise AssertionError("Read beyond maximum")
        self.assertEqual(len(await cache.get_many(ids())), 512)

    async def test_close_discards_inflight_result(self):
        started, release = threading.Event(), threading.Event()
        def fetch(qq):
            started.set()
            release.wait(2)
            return png()
        cache = self.cache(fetch=fetch, wait_seconds=.02)
        self.addCleanup(release.set)
        await cache.get_many(["111"])
        tasks = tuple(cache._tasks.values())
        cache.close()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(cache._cache, {})
        self.assertEqual(cache._tasks, {})


if __name__ == "__main__":
    unittest.main()