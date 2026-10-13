import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote_plus, urlsplit
from uuid import uuid4

from .common import (
    CommandContext,
    NOT_FOUND_IMAGE_PATH,
    at_segment,
    command_argument,
    image_segment_from_bytes,
    image_segment_from_file,
    text_segment,
)


LOGGER = logging.getLogger("qq-bot")
GD_MUSIC_API_URL = os.getenv("GD_MUSIC_API_URL", "https://music-api.gdstudio.xyz/api.php").rstrip("/")
GD_MUSIC_SITE_URL = os.getenv("GD_MUSIC_SITE_URL", "https://music.gdstudio.xyz").rstrip("/")
SONG_MUSIC_SOURCE = os.getenv("SONG_MUSIC_SOURCE", "netease")
SONG_BITRATE = os.getenv("SONG_BITRATE", "320")
SONG_QUALITY_CHOICES = ("192", "320", "740", "999")
# 选 740/999（无损档）时，上游实际返回码率视为“符合该档”的下限
SONG_QUALITY_FLOOR_BPS = {"192": 192, "320": 320, "740": 740, "999": 740}
SONG_COVER_SIZE = os.getenv("SONG_COVER_SIZE", "300")
SONG_COVER_MAX_BYTES = 5 * 1024 * 1024
SONG_DOWNLOAD_DIR = Path(__file__).resolve().parents[1] / "downloads"
SONG_MAX_DOWNLOAD_BYTES = int(os.getenv("SONG_MAX_DOWNLOAD_MB", "100")) * 1024 * 1024
# 超过该大小的文件不走 base64 单帧上传（避免超过 NapCat ws 100MiB 帧上限），改走 stream 分片上传
SONG_BASE64_UPLOAD_LIMIT_BYTES = int(os.getenv("SONG_BASE64_UPLOAD_LIMIT_MB", "45")) * 1024 * 1024
SONG_STREAM_CHUNK_BYTES = int(os.getenv("SONG_STREAM_CHUNK_KB", "256")) * 1024
SONG_SEARCH_LIMIT = 10
SONG_SESSION_SECONDS = int(os.getenv("SONG_SESSION_MINUTES", "10")) * 60
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg")


class SongApiError(RuntimeError):
    """GD 音乐台接口无法提供可用资源（频率限制、HTTP 错误等）。"""


class SongUnavailableError(SongApiError):
    """接口未返回可用下载链接（无版权、已下架、音质档位缺失）。"""


@dataclass(frozen=True)
class SongResult:
    song_id: str
    title: str
    artist: str
    album: str
    pic_id: str = ""
    source: str = SONG_MUSIC_SOURCE


@dataclass(frozen=True)
class SongDownload:
    url: str
    size_mb: float | None = None
    bitrate: int = 0


@dataclass(frozen=True)
class SongSearchSession:
    results: tuple[SongResult, ...]
    created_at: float
    site: str = "gd"  # "gd" / "siren"：搜索时一次锁定，该会话的选择都按此源下载


# song_siren 依赖本模块的共享类型/异常，故在共享定义完成后再导入；
# 用模块式导入（from . import）避免循环导入时名称未定义的问题。
from . import song_siren  # noqa: E402


def fetch_json(url: str, timeout: float = 30) -> Any:
    req = request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json",
            "Referer": f"{GD_MUSIC_SITE_URL}/",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except error.HTTPError as exc:
        if exc.code == 429:
            raise SongApiError("请求过于频繁，请稍后再试。") from exc
        raise SongApiError(f"音乐接口请求失败（HTTP {exc.code}）") from exc


def join_song_artists(raw_artist: Any) -> str:
    if isinstance(raw_artist, (list, tuple)):
        artists = [str(item).strip() for item in raw_artist if str(item).strip()]
        return "、".join(artists) or "未知歌手"
    return str(raw_artist or "").strip() or "未知歌手"


def parse_song_search_response(payload: Any) -> list[SongResult]:
    if not isinstance(payload, list):
        raise ValueError("Invalid GD music search response")
    songs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        songs.append(
            SongResult(
                song_id=str(item["id"]),
                title=str(item["name"]).strip(),
                artist=join_song_artists(item.get("artist")),
                album=str(item.get("album") or "").strip(),
                pic_id=str(item.get("pic_id") or "").strip(),
                source=str(item.get("source") or SONG_MUSIC_SOURCE).strip(),
            )
        )
    return songs


def search_songs(name: str, limit: int = SONG_SEARCH_LIMIT) -> list[SongResult]:
    query = quote_plus(name)
    payload = fetch_json(
        f"{GD_MUSIC_API_URL}?types=search&source={SONG_MUSIC_SOURCE}&name={query}&count={limit}&pages=1"
    )
    return parse_song_search_response(payload)[:limit]


def parse_song_size_mb(raw_size: Any) -> float | None:
    """API 文档称 size 单位为 KB，但实测为字节；小于 5e6 的值按 KB 理解。"""
    try:
        size = float(raw_size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    if size <= 5_000_000:
        size *= 1024
    return round(size / (1024 * 1024), 1)


def fetch_song_url(track_id: str, bitrate: str = SONG_BITRATE, source: str = SONG_MUSIC_SOURCE) -> SongDownload:
    payload = fetch_json(
        f"{GD_MUSIC_API_URL}?types=url&source={source}&id={quote_plus(track_id)}&br={bitrate}"
    )
    if not isinstance(payload, dict):
        raise SongUnavailableError("音乐接口返回异常")
    try:
        bitrate_value = int(payload.get("br"))
    except (TypeError, ValueError):
        bitrate_value = -1
    url = str(payload.get("url") or "").strip()
    if not url or bitrate_value == -1:
        raise SongUnavailableError("该歌曲暂时无法获取下载链接，可能无版权或已下架。")
    return SongDownload(url, parse_song_size_mb(payload.get("size")), max(bitrate_value, 0))


def song_quality_floor(quality: str) -> int:
    floor = SONG_QUALITY_FLOOR_BPS.get(quality)
    if floor is not None:
        return floor
    try:
        return max(int(quality), 0)
    except ValueError:
        return 0


def fetch_song_download(song: SongResult, quality: str = SONG_BITRATE) -> SongDownload:
    """按指定音质获取直链；当前音源不支持该音质时抛出可直接回复用户的提示。"""
    try:
        download = fetch_song_url(song.song_id, bitrate=quality, source=song.source)
    except SongUnavailableError:
        if quality == "320":
            raise
        # 用 320 档探针区分「歌曲不可用」与「仅不支持所选音质」
        try:
            fetch_song_url(song.song_id, bitrate="320", source=song.source)
        except SongApiError:
            raise SongUnavailableError("该歌曲暂时无法获取下载链接，可能无版权或已下架。") from None
        raise SongApiError(f"当前音源不支持所选音质（{quality}kbps），请降低音质后重试。") from None
    floor = song_quality_floor(quality)
    if floor and download.bitrate and download.bitrate < floor:
        raise SongApiError(
            f"当前音源不支持所选音质（{quality}kbps，实际仅支持 {download.bitrate}kbps），请降低音质后重试。"
        )
    return download


def fetch_song_cover(pic_id: str, source: str = SONG_MUSIC_SOURCE) -> bytes | None:
    """获取专辑封面图片字节；任何失败都返回 None，不阻断主流程。"""
    if not pic_id:
        return None
    try:
        payload = fetch_json(
            f"{GD_MUSIC_API_URL}?types=pic&source={source}&id={quote_plus(pic_id)}&size={SONG_COVER_SIZE}"
        )
        if not isinstance(payload, dict):
            return None
        cover_url = str(payload.get("url") or "").strip()
        if not cover_url:
            return None
        req = request.Request(
            cover_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Referer": f"{GD_MUSIC_SITE_URL}/",
            },
        )
        with request.urlopen(req, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/"):
                return None
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > SONG_COVER_MAX_BYTES:
                return None
            data = response.read(SONG_COVER_MAX_BYTES + 1)
        if len(data) > SONG_COVER_MAX_BYTES:
            return None
        return data
    except Exception:
        LOGGER.warning("Failed to fetch song cover for pic_id=%s", pic_id, exc_info=True)
        return None


def parse_song_selection(argument: str) -> tuple[int, str | None] | None:
    """解析 `2`、`选择 3`、`2+320`、`选择 2+999`，返回 (序号, 音质|None)。"""
    match = re.fullmatch(r"(?:选择\s*)?(\d+)\s*(?:\+?\s*(\d+))?", argument.strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


SIREN_SITE_NAME = "塞壬唱片"
SIREN_SITE_SUFFIXES = (f"+{SIREN_SITE_NAME}", f"＋{SIREN_SITE_NAME}")


def split_site_suffix(argument: str) -> tuple[str, bool]:
    """剥离结尾的 `+塞壬唱片`（含全角＋），返回 (剩余参数, 是否带站点后缀)。"""
    text = argument.strip()
    for marker in SIREN_SITE_SUFFIXES:
        if text.endswith(marker):
            return text[: -len(marker)].strip(), True
    return text, False


def parse_song_site_suffix(argument: str) -> tuple[str, str] | None:
    """识别**搜索关键词**结尾的 `+塞壬唱片`，返回 (关键词, "siren")；无后缀返回 None。"""
    keyword, has_site = split_site_suffix(argument)
    if not has_site or not keyword:
        return None
    if parse_song_selection(keyword) is not None:
        return None
    return keyword, "siren"


def format_song_search_results(results: list[SongResult], site: str = "gd") -> str:
    lines = ["搜索到以下歌曲："]
    for index, song in enumerate(results, 1):
        album = f"，专辑：{song.album}" if song.album else ""
        lines.append(f"{index}. {song.title} - {song.artist}{album}")
    lines.append("请回复：@bot /song 序号（例如：@bot /song 2），亦可回复 @bot /song 序号+音质（如 2+320）")
    if site == "siren":
        lines.append("（塞壬唱片候选可直接回复序号下载 WAV）")
    return "\n".join(lines)


def song_quality_label(bitrate: int) -> str:
    if bitrate <= 0:
        return "未知音质"
    label = f"{bitrate}kbps"
    if bitrate >= 740:
        label += "（无损）"
    return label


def song_quality_display(song: SongResult, download: SongDownload) -> str:
    """音质展示：塞壬唱片源固定 WAV 无损；其余按上游实际码率。"""
    if download.bitrate <= 0 and song.source == "monster-siren":
        return "WAV 无损"
    return song_quality_label(download.bitrate)


def format_song_details(song: SongResult, download: SongDownload) -> str:
    album = song.album or "未知"
    size = f"（约 {download.size_mb:.1f} MB）" if download.size_mb else ""
    return f"歌曲：{song.title}\n歌手：{song.artist}\n专辑：{album}\n音质：{song_quality_display(song, download)}{size}"


def detect_song_extension(path: Path, url: str = "") -> str:
    """按文件头魔术字节识别音频格式，退化时按 URL 后缀，最终兜底 .mp3。"""
    header = b""
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        header = b""
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
        return ".mp3"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"RIFF"):
        return ".wav"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return ".m4a"
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in AUDIO_EXTENSIONS else ".mp3"


def safe_song_filename(title: str, extension: str = ".mp3") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    suffix = extension if extension.startswith(".") else f".{extension}"
    return f"{(cleaned or 'song')[:80]}{suffix}"


def download_song_source(
    url: str,
    output_path: Path,
    max_bytes: int = SONG_MAX_DOWNLOAD_BYTES,
    referer: str = f"{GD_MUSIC_SITE_URL}/",
) -> None:
    """Stream a remote song to disk while enforcing a size limit."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": referer,
    }
    req = request.Request(url, headers=headers)
    total = 0
    with request.urlopen(req, timeout=60) as response, output_path.open("wb") as output:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise ValueError(f"Download URL returned {content_type}, not an audio file")
        content_length = int(response.headers.get("Content-Length", "0") or 0)
        if content_length > max_bytes:
            raise ValueError("Song file exceeds configured download limit")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Song file exceeds configured download limit")
            output.write(chunk)


def file_sha256(path: Path) -> str:
    """流式计算整文件 SHA256（分片上传协议需要完整哈希，不整文件入内存）。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def precheck_song_size(url: str, known_size_mb: float | None) -> float | None:
    """下载前估算文件大小（MB）：已知大小（GD 元数据）直接用；否则 HEAD Content-Length；失败返回 None。"""
    if known_size_mb:
        return known_size_mb
    try:
        req = request.Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Referer": f"{GD_MUSIC_SITE_URL}/",
            },
        )
        with request.urlopen(req, timeout=20) as response:
            content_length = int(response.headers.get("Content-Length", "0") or 0)
        if content_length > 0:
            return round(content_length / (1024 * 1024), 1)
    except Exception:
        LOGGER.warning("Failed to precheck song size for %s", url[:80], exc_info=True)
    return None


class SongPlugin:
    def __init__(self) -> None:
        self.song_sessions: dict[str, SongSearchSession] = {}

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == "/song"

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        argument = command_argument(context.text)
        if not argument:
            await bot._send_reply(
                websocket,
                event,
                "用法：@bot /song 歌曲名称；搜索后使用 @bot /song 序号（如 @bot /song 2）"
                "或 @bot /song 序号+音质（如 @bot /song 2+320，音质可选 "
                f"{'/'.join(SONG_QUALITY_CHOICES)}）下载。"
                "搜索塞壬唱片音乐请加后缀，如：@bot /song 歌曲名称+塞壬唱片。",
            )
            return

        session_key = self._song_session_key(event)
        if argument.lower() in {"取消", "cancel"}:
            self.song_sessions.pop(session_key, None)
            await bot._send_reply(websocket, event, "已取消本次歌曲选择。")
            return

        rest, has_site_suffix = split_site_suffix(argument)
        if has_site_suffix:
            selection_info = parse_song_selection(rest)
            if selection_info is not None:
                # 下载侧带塞壬后缀：是否放行由会话来源决定（_handle_song_selection 内校验）
                selection, quality = selection_info
                if quality is not None and quality not in SONG_QUALITY_CHOICES:
                    await bot._send_reply(
                        websocket,
                        event,
                        f"音质可选 {' / '.join(SONG_QUALITY_CHOICES)}，请重新输入，例如：@bot /song 2+320",
                    )
                    return
                await self._handle_song_selection(
                    bot, websocket, event, session_key, selection, quality, requested_site="siren"
                )
                return
            if rest:
                await self._search_siren_songs(bot, websocket, event, session_key, rest)
                return
            # 后缀前为空（如 `/song +塞壬唱片`）时按原参数走默认源搜索

        selection_info = parse_song_selection(argument)
        if selection_info is not None:
            selection, quality = selection_info
            if quality is not None and quality not in SONG_QUALITY_CHOICES:
                await bot._send_reply(
                    websocket,
                    event,
                    f"音质可选 {' / '.join(SONG_QUALITY_CHOICES)}，请重新输入，例如：@bot /song 2+320",
                )
                return
            await self._handle_song_selection(bot, websocket, event, session_key, selection, quality)
            return

        try:
            songs = await asyncio.to_thread(search_songs, argument)
        except SongApiError as exc:
            await bot._send_reply(websocket, event, str(exc))
            return
        except Exception:
            LOGGER.exception("Song search failed")
            await self._send_song_not_found(bot, websocket, event)
            return

        if not songs:
            await self._send_song_not_found(bot, websocket, event)
            return

        self.song_sessions[session_key] = SongSearchSession(tuple(songs), time.time(), site="gd")
        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_search_results(songs, site="gd")))
        await bot._send_reply(websocket, event, message)

    async def _search_siren_songs(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        session_key: str,
        keyword: str,
    ) -> None:
        try:
            songs = await asyncio.to_thread(song_siren.search_siren_songs, keyword)
        except SongApiError as exc:
            await bot._send_reply(websocket, event, str(exc))
            return
        except Exception:
            LOGGER.exception("Siren song search failed")
            await self._send_song_not_found(bot, websocket, event)
            return

        if not songs:
            await self._send_song_not_found(bot, websocket, event)
            return

        self.song_sessions[session_key] = SongSearchSession(tuple(songs), time.time(), site="siren")
        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_search_results(songs, site="siren")))
        await bot._send_reply(websocket, event, message)

    async def _handle_song_selection(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        session_key: str,
        selection: int,
        quality: str | None,
        requested_site: str | None = None,
    ) -> None:
        session = self.song_sessions.get(session_key)
        if not session or time.time() - session.created_at > SONG_SESSION_SECONDS:
            self.song_sessions.pop(session_key, None)
            if requested_site == "siren":
                await bot._send_reply(websocket, event, "歌曲候选已过期，请重新输入：@bot /song 歌曲名称+塞壬唱片")
            else:
                await bot._send_reply(websocket, event, "歌曲候选已过期，请重新输入：@bot /song 歌曲名称")
            return
        if requested_site == "siren" and session.site != "siren":
            # 搜索时没加后缀的会话，当次不支持从塞壬源下载
            await bot._send_reply(
                websocket,
                event,
                "当前候选来自默认音乐源；塞壬唱片请重新搜索：@bot /song 关键词+塞壬唱片",
            )
            return
        if selection < 1 or selection > len(session.results):
            await bot._send_reply(websocket, event, f"请选择 1 到 {len(session.results)} 之间的序号。")
            return

        selected = session.results[selection - 1]
        if session.site == "siren":
            if quality is not None:
                # 塞壬源只有 WAV 一种格式；拒绝且不消耗会话，用户可改用裸序号重试
                await bot._send_reply(
                    websocket,
                    event,
                    f"当前为塞壬唱片源，仅提供 WAV 无损，无音质选择；请直接 @bot /song {selection} 下载。",
                )
                return
            self.song_sessions.pop(session_key, None)
            await self._download_siren_song(bot, websocket, event, selected)
            return
        self.song_sessions.pop(session_key, None)
        await self._download_gd_song(bot, websocket, event, selected, quality)

    async def _download_gd_song(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        selected: SongResult,
        quality: str | None,
    ) -> None:
        try:
            download, cover_bytes = await asyncio.gather(
                asyncio.to_thread(fetch_song_download, selected, quality or SONG_BITRATE),
                asyncio.to_thread(fetch_song_cover, selected.pic_id, selected.source),
            )
        except SongApiError as exc:
            LOGGER.warning("Song API rejected selection: %s", exc)
            await bot._send_reply(websocket, event, str(exc))
            return
        except Exception:
            LOGGER.exception("Song download request failed")
            await bot._send_reply(websocket, event, "歌曲信息获取失败，请稍后重新搜索。")
            return

        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_details(selected, download)))
        if cover_bytes:
            message.append(image_segment_from_bytes(cover_bytes))
        await bot._send_reply(websocket, event, message)

        file_path = await self._announce_and_download(bot, websocket, event, selected, download)
        if not file_path:
            await bot._send_reply(websocket, event, "歌曲下载失败，请稍后再试。")
            return
        try:
            sent = await self._send_song_file(bot, websocket, event, file_path, f"{selected.title} - {selected.artist}")
            if not sent:
                await bot._send_reply(websocket, event, "文件上传失败，请稍后重试或前往原站下载。")
        finally:
            file_path.unlink(missing_ok=True)

    async def _announce_and_download(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        selected: SongResult,
        download: SongDownload,
        referer: str = f"{GD_MUSIC_SITE_URL}/",
    ) -> Path | None:
        """大文件下载前预告耗时，然后下载源文件。"""
        size_mb = await asyncio.to_thread(precheck_song_size, download.url, download.size_mb)
        if size_mb is not None and size_mb * 1024 * 1024 > SONG_BASE64_UPLOAD_LIMIT_BYTES:
            await bot._send_reply(
                websocket,
                event,
                f"文件较大（约 {size_mb:.0f} MB），下载和上传可能需要较久，请稍候…",
            )
        await bot._send_reply(websocket, event, f"正在下载源文件：{song_quality_display(selected, download)}")
        return await self._download_song_file(bot, download.url, referer=referer)

    async def _download_siren_song(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        selected: SongResult,
    ) -> None:
        try:
            download, cover_bytes = await asyncio.gather(
                asyncio.to_thread(song_siren.fetch_siren_url, selected),
                asyncio.to_thread(song_siren.fetch_siren_cover, selected.pic_id),
            )
        except SongApiError as exc:
            LOGGER.warning("Siren API rejected selection: %s", exc)
            await bot._send_reply(websocket, event, str(exc))
            return
        except Exception:
            LOGGER.exception("Siren song url request failed")
            await bot._send_reply(websocket, event, "歌曲信息获取失败，请稍后重新搜索。")
            return

        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_details(selected, download)))
        if cover_bytes:
            message.append(image_segment_from_bytes(cover_bytes))
        await bot._send_reply(websocket, event, message)

        file_path = await self._announce_and_download(
            bot, websocket, event, selected, download, referer=f"{song_siren.SIREN_SITE_URL}/"
        )
        if not file_path:
            await bot._send_reply(websocket, event, "歌曲下载失败，请稍后再试。")
            return
        try:
            sent = await self._send_song_file(bot, websocket, event, file_path, f"{selected.title} - {selected.artist}")
            if not sent:
                await bot._send_reply(websocket, event, "文件上传失败，请稍后重试或前往原站下载。")
        finally:
            file_path.unlink(missing_ok=True)

    def _song_session_key(self, event: dict[str, Any]) -> str:
        scope = event.get("group_id") if event.get("message_type") == "group" else "private"
        return f"{scope}:{event.get('user_id')}"

    async def _download_song_file(self, bot: Any, audio_url: str, referer: str = f"{GD_MUSIC_SITE_URL}/") -> Path | None:
        SONG_DOWNLOAD_DIR.mkdir(exist_ok=True)
        identifier = uuid4().hex
        source_path = SONG_DOWNLOAD_DIR / f"{identifier}.source"
        output_path: Path | None = None
        try:
            await asyncio.to_thread(download_song_source, audio_url, source_path, referer=referer)
            extension = detect_song_extension(source_path, audio_url)
            output_path = SONG_DOWNLOAD_DIR / f"{identifier}{extension}"
            source_path.rename(output_path)
            return output_path
        except Exception:
            LOGGER.exception("Failed to download song file")
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            return None
        finally:
            source_path.unlink(missing_ok=True)

    async def _send_song_file(self, bot: Any, websocket: Any, event: dict[str, Any], file_path: Path, title: str) -> bool:
        """发送歌曲文件：小文件走 base64 单帧；大文件走 NapCat Stream API 分片上传。"""
        name = safe_song_filename(title, file_path.suffix or ".mp3")
        if file_path.stat().st_size > SONG_BASE64_UPLOAD_LIMIT_BYTES:
            napcat_path = await self._stream_upload_to_napcat(bot, websocket, file_path, name)
            if not napcat_path:
                return False
            params: dict[str, Any] = {"file": napcat_path, "name": name}
        else:
            encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
            params = {"file": f"base64://{encoded}", "name": name}
        if event.get("message_type") == "group":
            params["group_id"] = int(event["group_id"])
            action = "upload_group_file"
        else:
            params["user_id"] = int(event["user_id"])
            action = "upload_private_file"
        await bot._send_action(websocket, action, params)
        return True

    async def _stream_upload_to_napcat(self, bot: Any, websocket: Any, file_path: Path, name: str) -> str | None:
        """大文件分片上传到 NapCat（v4.8.115+ Stream API），返回 NapCat 侧文件路径；失败返回 None。"""
        stream_id = uuid4().hex
        total_size = file_path.stat().st_size
        total_chunks = max(1, math.ceil(total_size / SONG_STREAM_CHUNK_BYTES))
        digest = await asyncio.to_thread(file_sha256, file_path)
        try:
            with file_path.open("rb") as handle:
                for index in range(total_chunks):
                    chunk = handle.read(SONG_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    await bot._send_action_request(
                        websocket,
                        "upload_file_stream",
                        {
                            "stream_id": stream_id,
                            "chunk_data": base64.b64encode(chunk).decode("ascii"),
                            "chunk_index": index,
                            "total_chunks": total_chunks,
                            "file_size": total_size,
                            "expected_sha256": digest,
                            "filename": name,
                            "file_retention": 60_000,
                        },
                        timeout=30,
                    )
            done = await bot._send_action_request(
                websocket,
                "upload_file_stream",
                {"stream_id": stream_id, "is_complete": True},
                timeout=60,
            )
        except Exception:
            LOGGER.exception("Stream upload failed for %s (%s bytes, chunk=%s)", name, total_size, SONG_STREAM_CHUNK_BYTES)
            return None
        napcat_path = str((done.get("data") or {}).get("file_path") or "")
        if not napcat_path:
            LOGGER.warning("Stream upload completion response missing file_path: %s", done)
            return None
        return napcat_path

    async def _send_song_not_found(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        message = [text_segment("私密马赛，没有找到喵~")]
        if NOT_FOUND_IMAGE_PATH.exists():
            message.append(image_segment_from_file(NOT_FOUND_IMAGE_PATH))
        await bot._send_reply(websocket, event, message)
