import base64
import asyncio
import hashlib
import os
import time
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.drivers import Response


def group_event(command: str, *, user_id=111, group_id=100, message_id=50):
    message = Message([MessageSegment.at("999"), MessageSegment.text(f" {command}")])
    return GroupMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "normal",
        "user_id": user_id, "message_type": "group", "message_id": message_id,
        "group_id": group_id, "message": message, "original_message": message.copy(),
        "raw_message": str(message), "font": 0,
        "sender": {"user_id": user_id, "nickname": "tester", "card": "", "role": "member"},
    })


def create_bot(context):
    return context.create_bot(
        base=Bot, adapter=context.create_adapter(base=Adapter), self_id="999"
    )


@pytest.fixture(autouse=True)
def reset_song_state():
    from plugins import song

    song.reset_state()
    yield
    song.reset_state()


@pytest.mark.asyncio
async def test_song_search_cancel_and_group_user_scope(app, monkeypatch):
    from plugins import song as module

    result = module.SongResult("1", "测试歌曲", "测试歌手", "测试专辑", "pic", "netease")
    search = AsyncMock(return_value=[result])
    monkeypatch.setattr(module, "search_songs", search)
    event = group_event("/song 测试")
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(
            event,
            module._addressed_message(event, module.format_song_search_results([result])),
            bot=bot,
        )
    search.assert_awaited_once()
    assert module.song_sessions["100:111"].results == (result,)

    cancel = group_event("/song cancel", user_id=111, group_id=100, message_id=51)
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, cancel)
        context.should_pass_rule()
        context.should_call_send(cancel, "已取消本次歌曲选择。", bot=bot)
    assert "100:111" not in module.song_sessions

    module.song_sessions["100:111"] = module.SongSearchSession((result,), time.time())
    other_group = group_event("/song 1", user_id=111, group_id=200, message_id=52)
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, other_group)
        context.should_pass_rule()
        context.should_call_send(
            other_group, "歌曲候选已过期，请重新输入：@bot /song 歌曲名称", bot=bot
        )


@pytest.mark.asyncio
async def test_song_selection_expiry_range_and_quality_boundaries(app):
    from plugins import song as module

    result = module.SongResult("1", "测试歌曲", "测试歌手", "", "", "netease")
    module.song_sessions["100:111"] = module.SongSearchSession((result,), 0)
    expired = group_event("/song 1")
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, expired)
        context.should_pass_rule()
        context.should_call_send(
            expired, "歌曲候选已过期，请重新输入：@bot /song 歌曲名称", bot=bot
        )

    invalid_quality = group_event("/song 1+128", message_id=53)
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, invalid_quality)
        context.should_pass_rule()
        context.should_call_send(
            invalid_quality,
            "音质可选 192 / 320 / 740 / 999，请重新输入，例如：@bot /song 2+320",
            bot=bot,
        )

    module.song_sessions["100:111"] = module.SongSearchSession((result,), time.time())
    out_of_range = group_event("/song 2", message_id=54)
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, out_of_range)
        context.should_pass_rule()
        context.should_call_send(out_of_range, "请选择 1 到 1 之间的序号。", bot=bot)
    assert "100:111" in module.song_sessions


@pytest.mark.asyncio
async def test_song_download_failure_returns_direct_link_and_consumes_session(app, monkeypatch):
    from plugins import song as module

    selected = module.SongResult("1", "测试歌曲", "歌手", "专辑", "", "netease")
    download = module.SongDownload("https://cdn.example/song.mp3", 12.5, 320)
    module.song_sessions["100:111"] = module.SongSearchSession((selected,), time.time())
    monkeypatch.setattr(module, "fetch_song_download", AsyncMock(return_value=download))
    monkeypatch.setattr(module, "fetch_song_cover", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "_announce_and_download", AsyncMock(return_value=None))
    event = group_event("/song 1")
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(
            event,
            module._addressed_message(event, module.format_song_details(selected, download)),
            bot=bot,
        )
        context.should_call_send(
            event, "下载失败，请点击直链自行下载：https://cdn.example/song.mp3", bot=bot
        )
    assert "100:111" not in module.song_sessions


@pytest.mark.asyncio
async def test_siren_session_rejects_quality_without_consuming_session(app):
    from plugins import song as module

    selected = module.SongResult("1", "塞壬歌曲", "塞壬唱片-MSR", "专辑", "a", "monster-siren")
    module.song_sessions["100:111"] = module.SongSearchSession((selected,), time.time(), "siren")
    event = group_event("/song 1+320")
    async with app.test_matcher(module.song) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(
            event,
            "当前为塞壬唱片源，仅提供 WAV 无损，无音质选择；请直接 @bot /song 1 下载。",
            bot=bot,
        )
    assert "100:111" in module.song_sessions


@pytest.mark.asyncio
async def test_small_file_uses_base64_group_upload(tmp_path):
    from plugins import song as module

    file_path = tmp_path / "song.mp3"
    file_path.write_bytes(b"ID3test")
    bot = AsyncMock()
    event = group_event("/song 1")
    config = module.Config(song_download_dir=tmp_path, song_base64_upload_limit_mb=1)
    assert await module._send_song_file(bot, event, file_path, "歌名 - 歌手", config)
    bot.call_api.assert_awaited_once()
    api, = bot.call_api.await_args.args
    params = bot.call_api.await_args.kwargs
    assert api == "upload_group_file"
    assert params["group_id"] == 100
    assert params["file"] == f"base64://{base64.b64encode(b'ID3test').decode('ascii')}"
    assert params["name"].endswith(".mp3")


@pytest.mark.asyncio
async def test_large_file_streams_full_hash_then_uploads_napcat_path(tmp_path):
    from plugins import song as module

    data = (b"0123456789abcdef" * 65537)[: 1024 * 1024 + 17]
    file_path = tmp_path / "song.wav"
    file_path.write_bytes(data)
    calls = []

    async def call_api(api, **params):
        calls.append((api, params))
        if api == "upload_file_stream" and params.get("is_complete"):
            return {"file_path": "/tmp/napcat/streamed.wav"}
        return {}

    bot = AsyncMock()
    bot.call_api.side_effect = call_api
    config = module.Config(
        song_download_dir=tmp_path,
        song_base64_upload_limit_mb=1,
        song_stream_chunk_kb=64,
    )
    assert await module._send_song_file(bot, group_event("/song 1"), file_path, "大文件", config)
    stream_chunks = [params for api, params in calls if api == "upload_file_stream" and "chunk_data" in params]
    assert len(stream_chunks) == 17
    assert {params["expected_sha256"] for params in stream_chunks} == {hashlib.sha256(data).hexdigest()}
    assert [params["chunk_index"] for params in stream_chunks] == list(range(17))
    assert calls[-1] == (
        "upload_group_file",
        {"group_id": 100, "file": "/tmp/napcat/streamed.wav", "name": "大文件.wav"},
    )


@pytest.mark.asyncio
async def test_stream_failure_and_missing_completion_path_return_false(tmp_path):
    from plugins import song as module

    file_path = tmp_path / "song.wav"
    file_path.write_bytes(b"x" * (1024 * 1024 + 1))
    config = module.Config(
        song_download_dir=tmp_path,
        song_base64_upload_limit_mb=1,
        song_stream_chunk_kb=64,
    )

    failed_bot = AsyncMock()
    failed_bot.call_api.side_effect = RuntimeError("stream failed")
    assert not await module._send_song_file(
        failed_bot, group_event("/song 1"), file_path, "失败", config
    )

    missing_bot = AsyncMock()
    missing_bot.call_api.return_value = {}
    assert not await module._send_song_file(
        missing_bot, group_event("/song 1"), file_path, "缺少路径", config
    )


@pytest.mark.asyncio
async def test_driver_stream_download_enforces_limit_and_plugin_cleans_partial(tmp_path, monkeypatch):
    from plugins import song as module
    from services.song import SongApiError, download_song_source

    class FakeDriver:
        async def stream_request(self, request, *, chunk_size):
            yield Response(
                200,
                headers={"Content-Type": "audio/mpeg", "Content-Length": "6"},
                content=b"abc",
            )
            yield Response(
                200,
                headers={"Content-Type": "audio/mpeg", "Content-Length": "6"},
                content=b"def",
            )

    output = tmp_path / "ok.source"
    await download_song_source(
        FakeDriver(), "https://example/song", output, max_bytes=6, referer="https://example/"
    )
    assert output.read_bytes() == b"abcdef"
    with pytest.raises(SongApiError, match="大小限制"):
        await download_song_source(
            FakeDriver(), "https://example/song", tmp_path / "large.source",
            max_bytes=5, referer="https://example/",
        )

    async def partial_failure(driver, url, output_path, *, max_bytes, referer):
        output_path.write_bytes(b"partial")
        raise OSError("network stopped")

    monkeypatch.setattr(module, "download_song_source", partial_failure)
    monkeypatch.setattr(module, "get_driver", lambda: object())
    config = module.Config(song_download_dir=tmp_path)
    assert await module._download_song_file("https://example/song", "https://example/", config) is None
    assert not list((tmp_path / "song-cache").glob("song-*"))
    assert not module.active_temp_files


@pytest.mark.asyncio
async def test_song_download_cache_reuses_file_and_deduplicates_concurrent_requests(tmp_path, monkeypatch):
    from plugins import song as module

    calls = 0

    async def download(driver, url, output_path, *, max_bytes, referer):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        output_path.write_bytes(b"ID3cached")

    monkeypatch.setattr(module, "download_song_source", download)
    monkeypatch.setattr(module, "get_driver", lambda: object())
    config = module.Config(song_download_dir=tmp_path)
    results = await asyncio.gather(*(
        module._download_song_file("https://example/song.mp3", "https://example/", config, cache_key="same")
        for _ in range(3)
    ))
    assert calls == 1
    assert results[0] == results[1] == results[2]
    assert results[0] is not None and results[0].read_bytes() == b"ID3cached"
    assert await module._download_song_file(
        "https://example/song.mp3", "https://example/", config, cache_key="same"
    ) == results[0]
    assert calls == 1


@pytest.mark.asyncio
async def test_song_download_retries_network_error_but_not_api_error(tmp_path, monkeypatch):
    from plugins import song as module
    from services.song import SongApiError

    attempts = 0

    async def flaky(driver, url, output_path, *, max_bytes, referer):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary DNS failure")
        output_path.write_bytes(b"ID3retry")

    monkeypatch.setattr(module, "download_song_source", flaky)
    monkeypatch.setattr(module, "get_driver", lambda: object())
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    config = module.Config(song_download_dir=tmp_path)
    result = await module._download_song_file("https://example/retry", "https://example/", config, cache_key="retry")
    assert result is not None and attempts == 2

    async def rejected(driver, url, output_path, *, max_bytes, referer):
        nonlocal attempts
        attempts = 0
        raise SongApiError("rejected")

    monkeypatch.setattr(module, "download_song_source", rejected)
    assert await module._download_song_file("https://example/rejected", "https://example/", config, cache_key="rejected") is None
    assert attempts == 0


def test_song_cache_cleanup_respects_ttl_and_active_upload(tmp_path):
    from plugins import song as module

    config = module.Config(song_download_dir=tmp_path, song_cache_ttl_hours=24)
    cache_dir = tmp_path / "song-cache"
    cache_dir.mkdir()
    expired = cache_dir / "song-expired.mp3"
    expired.write_bytes(b"old")
    os.utime(expired, (time.time() - 25 * 3600, time.time() - 25 * 3600))
    active = cache_dir / "song-active.mp3"
    active.write_bytes(b"active")
    module.active_cache_files.add(active)
    module.cleanup_song_cache(config)
    assert not expired.exists()
    assert active.exists()


def test_song_parsers_and_runtime_config_mapping(tmp_path):
    from infrastructure.config import RuntimeConfig
    from services.song import (
        SongResult,
        parse_song_selection,
        parse_song_site_suffix,
        safe_song_filename,
        song_quality_label,
    )

    assert parse_song_selection("选择 2+999") == (2, "999")
    assert parse_song_selection("2 740") == (2, "740")
    assert parse_song_site_suffix("大地之盐+塞壬唱片") == ("大地之盐", "siren")
    assert safe_song_filename('A/B: C?', ".flac") == "A_B_ C.flac"
    assert song_quality_label(740) == "740kbps（无损）"
    assert SongResult("1", "歌", "歌手", "专辑").source == "netease"

    options = RuntimeConfig(song_download_dir=tmp_path.resolve()).nonebot_options({}, {})
    assert options["song_download_dir"] == tmp_path.resolve()
    assert options["song_bitrate"] == "320"
    assert options["song_stream_chunk_kb"] == 256

