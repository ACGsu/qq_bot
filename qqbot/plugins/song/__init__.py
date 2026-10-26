"""Native song search, selection, download and NapCat file upload plugin."""
import asyncio
import base64
import hashlib
import logging
import math
import os
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from nonebot import get_driver, get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel, Field

from infrastructure.runtime import runtime
from infrastructure.onebot import command_argument
from plugins.core import command_context_after_real_mention
from services.song import (
    AUDIO_EXTENSIONS,
    SONG_QUALITY_CHOICES,
    SONG_SEARCH_LIMIT,
    SongApiError,
    SongDownload,
    SongResult,
    SongSearchSession,
    SongServiceConfig,
    SongUnavailableError,
    detect_song_extension,
    download_song_source,
    fetch_siren_cover,
    fetch_siren_url,
    fetch_song_cover,
    fetch_song_download,
    fetch_song_url,
    format_song_details,
    format_song_search_results,
    parse_song_search_response,
    parse_song_selection,
    parse_song_site_suffix,
    parse_song_size_mb,
    precheck_song_size,
    reset_state as reset_service_state,
    safe_song_filename,
    search_siren_songs,
    search_songs,
    song_quality_display,
    song_quality_label,
    split_site_suffix,
)

LOGGER = logging.getLogger("qq-bot")

__plugin_meta__ = PluginMetadata(
    name="歌曲下载",
    description="搜索歌曲、选择音质并发送源文件，支持塞壬唱片 WAV",
    usage="@bot /song 歌曲名；@bot /song 序号；@bot /song 歌曲名+塞壬唱片",
)


class Config(BaseModel):
    song_music_source: str = "netease"
    song_bitrate: Literal["192", "320", "740", "999"] = "320"
    song_session_minutes: int = Field(default=10, ge=1, le=1440)
    song_max_download_mb: int = Field(default=100, ge=1, le=1024)
    song_base64_upload_limit_mb: int = Field(default=35, ge=1, le=100)
    song_stream_chunk_kb: int = Field(default=256, ge=64, le=4096)
    song_cache_ttl_hours: int = Field(default=24, ge=1, le=168)
    song_download_dir: Path = Path("storage/downloads").resolve()
    song_cover_size: str = "300"
    gd_music_api_url: str = "https://music-api.gdstudio.xyz/api.php"
    gd_music_site_url: str = "https://music.gdstudio.xyz"
    siren_api_base_url: str = "https://monster-siren.hypergryph.com"
    siren_catalog_ttl_minutes: int = Field(default=10, ge=1, le=1440)


song_sessions: dict[str, SongSearchSession] = {}
active_temp_files: set[Path] = set()
active_cache_files: set[Path] = set()
download_locks: dict[str, asyncio.Lock] = {}
cache_cleanup_task: asyncio.Task[None] | None = None
CACHE_CLEANUP_INTERVAL_SECONDS = 60 * 60
PARTIAL_FILE_MAX_AGE_SECONDS = 60 * 60


def _service_config(config: Config) -> SongServiceConfig:
    return SongServiceConfig(
        gd_api_url=config.gd_music_api_url.rstrip("/"),
        gd_site_url=config.gd_music_site_url.rstrip("/"),
        music_source=config.song_music_source,
        default_bitrate=config.song_bitrate,
        cover_size=config.song_cover_size,
        siren_api_url=config.siren_api_base_url.rstrip("/"),
        siren_catalog_ttl_seconds=config.siren_catalog_ttl_minutes * 60,
    )


def song_cache_key(song: SongResult, quality: str, *, site: str) -> str:
    """Return a stable cache key for one source/song/quality combination."""
    raw = "\0".join((site, song.source, str(song.song_id), quality))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _song_cache_dir(config: Config) -> Path:
    return config.song_download_dir / "song-cache"


def _cache_path_candidates(config: Config, cache_key: str) -> tuple[Path, ...]:
    directory = _song_cache_dir(config)
    return tuple(directory / f"song-{cache_key}{extension}" for extension in AUDIO_EXTENSIONS)


def find_cached_song(config: Config, cache_key: str, *, now: float | None = None) -> Path | None:
    current = time.time() if now is None else now
    ttl = config.song_cache_ttl_hours * 60 * 60
    max_bytes = config.song_max_download_mb * 1024 * 1024
    for path in _cache_path_candidates(config, cache_key):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if path in active_cache_files:
            return path if stat.st_size > 0 and stat.st_size <= max_bytes else None
        if stat.st_size <= 0 or stat.st_size > max_bytes or current - stat.st_mtime >= ttl:
            _remove_temp_file(path)
            continue
        return path
    return None


def cleanup_song_cache(config: Config, *, now: float | None = None) -> None:
    directory = _song_cache_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    current = time.time() if now is None else now
    ttl = config.song_cache_ttl_hours * 60 * 60
    max_bytes = config.song_max_download_mb * 1024 * 1024
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        LOGGER.warning("Unable to inspect song cache directory %s", directory, exc_info=True)
        return
    for path in entries:
        if path in active_temp_files or path in active_cache_files:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.suffix == ".part":
            expired = current - stat.st_mtime >= PARTIAL_FILE_MAX_AGE_SECONDS
        else:
            expired = path.name.startswith("song-") and (
                path.suffix not in AUDIO_EXTENSIONS
                or stat.st_size <= 0
                or stat.st_size > max_bytes
                or current - stat.st_mtime >= ttl
            )
        if expired:
            _remove_temp_file(path)


def _command_text(event: MessageEvent) -> str:
    context = command_context_after_real_mention(event)
    return context.text if context else ""


async def song_command(event: MessageEvent) -> bool:
    text = _command_text(event)
    return bool(text and text.split(maxsplit=1)[0] == "/song")


song = on_message(rule=Rule(song_command), priority=20, block=True)


def _session_key(event: MessageEvent) -> str:
    scope = str(event.group_id) if isinstance(event, GroupMessageEvent) else "private"
    return f"{scope}:{event.user_id}"


def _addressed_message(event: MessageEvent, text: str, cover: bytes | None = None) -> Message:
    message = Message()
    if isinstance(event, GroupMessageEvent):
        message += MessageSegment.at(str(event.user_id))
        message += MessageSegment.text(" ")
    message += MessageSegment.text(text)
    if cover:
        message += MessageSegment.image(cover)
    return message


async def _send_not_found(bot: Bot, event: MessageEvent) -> None:
    message = Message("私密马赛，没有找到喵~")
    image = runtime.config.assets_root / "not_found.jpg"
    if image.is_file():
        message += MessageSegment.image(image.read_bytes())
    await bot.send(event, message)


@song.handle()
async def handle_song(bot: Bot, event: MessageEvent) -> None:
    argument = command_argument(_command_text(event))
    if not argument:
        await bot.send(
            event,
            "用法：@bot /song 歌曲名称；搜索后使用 @bot /song 序号（如 @bot /song 2）"
            "或 @bot /song 序号+音质（如 @bot /song 2+320，音质可选 "
            f"{'/'.join(SONG_QUALITY_CHOICES)}）下载。"
            "搜索塞壬唱片音乐请加后缀，如：@bot /song 歌曲名称+塞壬唱片。",
        )
        return

    session_key = _session_key(event)
    if argument.lower() in {"取消", "cancel"}:
        song_sessions.pop(session_key, None)
        await bot.send(event, "已取消本次歌曲选择。")
        return

    rest, has_site_suffix = split_site_suffix(argument)
    if has_site_suffix:
        selection_info = parse_song_selection(rest)
        if selection_info is not None:
            selection, quality = selection_info
            if not await _quality_is_valid(bot, event, quality):
                return
            await _handle_selection(
                bot, event, session_key, selection, quality, requested_site="siren"
            )
            return
        if rest:
            await _search(bot, event, session_key, rest, site="siren")
            return

    selection_info = parse_song_selection(argument)
    if selection_info is not None:
        selection, quality = selection_info
        if not await _quality_is_valid(bot, event, quality):
            return
        await _handle_selection(bot, event, session_key, selection, quality)
        return
    await _search(bot, event, session_key, argument, site="gd")


async def _quality_is_valid(bot: Bot, event: MessageEvent, quality: str | None) -> bool:
    if quality is None or quality in SONG_QUALITY_CHOICES:
        return True
    await bot.send(
        event,
        f"音质可选 {' / '.join(SONG_QUALITY_CHOICES)}，请重新输入，例如：@bot /song 2+320",
    )
    return False


async def _search(
    bot: Bot, event: MessageEvent, session_key: str, keyword: str, *, site: str
) -> None:
    config = get_plugin_config(Config)
    service_config = _service_config(config)
    try:
        if site == "siren":
            results = await search_siren_songs(get_driver(), service_config, keyword)
        else:
            results = await search_songs(get_driver(), service_config, keyword)
    except SongApiError as exc:
        await bot.send(event, str(exc))
        return
    except Exception:
        LOGGER.exception("%s song search failed", site)
        await _send_not_found(bot, event)
        return
    if not results:
        await _send_not_found(bot, event)
        return
    song_sessions[session_key] = SongSearchSession(tuple(results), time.time(), site=site)
    await bot.send(event, _addressed_message(event, format_song_search_results(results, site)))


async def _handle_selection(
    bot: Bot,
    event: MessageEvent,
    session_key: str,
    selection: int,
    quality: str | None,
    requested_site: str | None = None,
) -> None:
    config = get_plugin_config(Config)
    session = song_sessions.get(session_key)
    if not session or time.time() - session.created_at > config.song_session_minutes * 60:
        song_sessions.pop(session_key, None)
        suffix = "+塞壬唱片" if requested_site == "siren" else ""
        await bot.send(event, f"歌曲候选已过期，请重新输入：@bot /song 歌曲名称{suffix}")
        return
    if requested_site == "siren" and session.site != "siren":
        await bot.send(event, "当前候选来自默认音乐源；塞壬唱片请重新搜索：@bot /song 关键词+塞壬唱片")
        return
    if selection < 1 or selection > len(session.results):
        await bot.send(event, f"请选择 1 到 {len(session.results)} 之间的序号。")
        return

    selected = session.results[selection - 1]
    if session.site == "siren" and quality is not None:
        await bot.send(
            event,
            f"当前为塞壬唱片源，仅提供 WAV 无损，无音质选择；请直接 @bot /song {selection} 下载。",
        )
        return
    song_sessions.pop(session_key, None)
    await _download_selected(bot, event, selected, quality, site=session.site)


async def _download_selected(
    bot: Bot,
    event: MessageEvent,
    selected: SongResult,
    quality: str | None,
    *,
    site: str,
) -> None:
    config = get_plugin_config(Config)
    service_config = _service_config(config)
    driver = get_driver()
    requested_quality = "wav" if site == "siren" else (quality or config.song_bitrate)
    cache_key = song_cache_key(selected, requested_quality, site=site)
    cached_path = await asyncio.to_thread(find_cached_song, config, cache_key)
    if cached_path is not None:
        cover = await _fetch_selected_cover(driver, service_config, selected, site)
        cached_download = SongDownload(
            "",
            round(cached_path.stat().st_size / (1024 * 1024), 1),
            0 if site == "siren" else int(requested_quality),
        )
        await bot.send(
            event,
            _addressed_message(event, format_song_details(selected, cached_download), cover),
        )
        await bot.send(
            event,
            f"已命中 {config.song_cache_ttl_hours} 小时歌曲缓存，直接发送源文件："
            f"{song_quality_display(selected, cached_download)}",
        )
        await _send_selected_file(bot, event, cached_path, selected, config)
        return
    try:
        if site == "siren":
            download, cover = await asyncio.gather(
                fetch_siren_url(driver, service_config, selected),
                fetch_siren_cover(driver, service_config, selected.pic_id),
            )
            referer = f"{service_config.siren_api_url}/"
        else:
            download, cover = await asyncio.gather(
                fetch_song_download(
                    driver, service_config, selected, quality or config.song_bitrate
                ),
                fetch_song_cover(driver, service_config, selected.pic_id, selected.source),
            )
            referer = f"{service_config.gd_site_url}/"
    except SongApiError as exc:
        LOGGER.warning("Song API rejected selection: %s", exc)
        await bot.send(event, str(exc))
        return
    except Exception:
        LOGGER.exception("Song metadata request failed")
        await bot.send(event, "歌曲信息获取失败，请稍后重新搜索。")
        return

    await bot.send(event, _addressed_message(event, format_song_details(selected, download), cover))
    file_path = await _announce_and_download(
        bot, event, selected, download, referer, config, cache_key=cache_key
    )
    if file_path is None:
        await bot.send(event, f"下载失败，请点击直链自行下载：{download.url}")
        return
    await _send_selected_file(bot, event, file_path, selected, config)


async def _fetch_selected_cover(
    driver, service_config: SongServiceConfig, selected: SongResult, site: str
) -> bytes | None:
    if site == "siren":
        return await fetch_siren_cover(driver, service_config, selected.pic_id)
    return await fetch_song_cover(driver, service_config, selected.pic_id, selected.source)


async def _send_selected_file(
    bot: Bot, event: MessageEvent, file_path: Path, selected: SongResult, config: Config
) -> None:
    active_cache_files.add(file_path)
    try:
        sent = await _send_song_file(
            bot, event, file_path, f"{selected.title} - {selected.artist}", config
        )
        if not sent:
            await bot.send(event, "文件上传失败，请稍后重试或前往原站下载。")
    finally:
        active_cache_files.discard(file_path)


async def _announce_and_download(
    bot: Bot,
    event: MessageEvent,
    selected: SongResult,
    download: SongDownload,
    referer: str,
    config: Config,
    *,
    cache_key: str,
) -> Path | None:
    cached_path = await asyncio.to_thread(find_cached_song, config, cache_key)
    if cached_path is not None:
        await bot.send(
            event,
            f"已命中 {config.song_cache_ttl_hours} 小时歌曲缓存，直接发送源文件。",
        )
        return cached_path
    size_mb = await precheck_song_size(get_driver(), download.url, download.size_mb, referer=referer)
    if size_mb is not None and size_mb > config.song_base64_upload_limit_mb:
        await bot.send(event, f"文件较大（约 {size_mb:.0f} MB），下载和上传可能需要较久，请稍候…")
    await bot.send(event, f"正在下载源文件：{song_quality_display(selected, download)}")
    return await _download_song_file(download.url, referer, config, cache_key=cache_key)


async def _download_song_file(
    url: str,
    referer: str,
    config: Config,
    *,
    cache_key: str | None = None,
) -> Path | None:
    """Download one song into the persistent cache, with a single retry."""
    cache_key = cache_key or hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_dir = _song_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = find_cached_song(config, cache_key)
    if cached is not None:
        return cached
    lock = download_locks.setdefault(cache_key, asyncio.Lock())
    try:
        async with lock:
            cached = find_cached_song(config, cache_key)
            if cached is not None:
                return cached
            for attempt in range(2):
                source_path = cache_dir / f".song-{cache_key}-{uuid4().hex}.part"
                active_temp_files.add(source_path)
                try:
                    await download_song_source(
                        get_driver(),
                        url,
                        source_path,
                        max_bytes=config.song_max_download_mb * 1024 * 1024,
                        referer=referer,
                    )
                    extension = detect_song_extension(source_path, url)
                    output_path = cache_dir / f"song-{cache_key}{extension}"
                    os.replace(source_path, output_path)
                    active_temp_files.discard(source_path)
                    return output_path
                except SongApiError:
                    LOGGER.warning("Song download rejected for %s", url[:100])
                    _remove_temp_file(source_path)
                    return None
                except Exception:
                    LOGGER.exception("Failed to download song file (attempt %s/2)", attempt + 1)
                    _remove_temp_file(source_path)
                    if attempt == 0:
                        await asyncio.sleep(1)
                    else:
                        return None
            return None
    finally:
        if download_locks.get(cache_key) is lock:
            download_locks.pop(cache_key, None)


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _base64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


async def _send_song_file(
    bot: Bot, event: MessageEvent, file_path: Path, title: str, config: Config
) -> bool:
    name = safe_song_filename(title, file_path.suffix or ".mp3")
    try:
        if file_path.stat().st_size > config.song_base64_upload_limit_mb * 1024 * 1024:
            napcat_path = await _stream_upload_to_napcat(bot, file_path, name, config)
            if not napcat_path:
                return False
            file_value = napcat_path
        else:
            file_value = f"base64://{await asyncio.to_thread(_base64_file, file_path)}"
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "upload_group_file", group_id=event.group_id, file=file_value, name=name
            )
        else:
            await bot.call_api(
                "upload_private_file", user_id=event.user_id, file=file_value, name=name
            )
        return True
    except Exception:
        LOGGER.exception("Song file upload failed for %s", name)
        return False


def _napcat_file_path(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    direct = str(result.get("file_path") or "")
    if direct:
        return direct
    data = result.get("data")
    return str(data.get("file_path") or "") if isinstance(data, dict) else ""


async def _stream_upload_to_napcat(
    bot: Bot, file_path: Path, name: str, config: Config
) -> str | None:
    stream_id = uuid4().hex
    total_size = file_path.stat().st_size
    chunk_bytes = config.song_stream_chunk_kb * 1024
    total_chunks = max(1, math.ceil(total_size / chunk_bytes))
    digest = await asyncio.to_thread(file_sha256, file_path)
    try:
        with file_path.open("rb") as handle:
            for index in range(total_chunks):
                chunk = handle.read(chunk_bytes)
                if not chunk:
                    break
                await asyncio.wait_for(
                    bot.call_api(
                        "upload_file_stream",
                        stream_id=stream_id,
                        chunk_data=base64.b64encode(chunk).decode("ascii"),
                        chunk_index=index,
                        total_chunks=total_chunks,
                        file_size=total_size,
                        expected_sha256=digest,
                        filename=name,
                        file_retention=60_000,
                    ),
                    timeout=30,
                )
        result = await asyncio.wait_for(
            bot.call_api("upload_file_stream", stream_id=stream_id, is_complete=True),
            timeout=60,
        )
    except Exception:
        LOGGER.exception(
            "Stream upload failed for %s (%s bytes, chunk=%s)", name, total_size, chunk_bytes
        )
        return None
    path = _napcat_file_path(result)
    if not path:
        LOGGER.warning("Stream upload completion response missing file_path")
        return None
    return path


def _remove_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    finally:
        active_temp_files.discard(path)


def reset_state() -> None:
    song_sessions.clear()
    for path in tuple(active_temp_files):
        _remove_temp_file(path)
    active_cache_files.clear()
    download_locks.clear()
    reset_service_state()


async def _cache_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)
        try:
            config = get_plugin_config(Config)
            await asyncio.to_thread(cleanup_song_cache, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Song cache cleanup failed")


@get_driver().on_startup
async def startup() -> None:
    global cache_cleanup_task
    config = get_plugin_config(Config)
    await asyncio.to_thread(cleanup_song_cache, config)
    cache_cleanup_task = asyncio.create_task(_cache_cleanup_loop())


@get_driver().on_shutdown
async def shutdown() -> None:
    global cache_cleanup_task
    if cache_cleanup_task is not None:
        cache_cleanup_task.cancel()
        try:
            await cache_cleanup_task
        except asyncio.CancelledError:
            pass
        cache_cleanup_task = None
    reset_state()
