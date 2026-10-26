"""Song search, metadata and streamed downloads through NoneBot's HTTP driver."""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

from nonebot.drivers import Request

LOGGER = logging.getLogger("qq-bot")
SONG_QUALITY_CHOICES = ("192", "320", "740", "999")
SONG_QUALITY_FLOOR_BPS = {"192": 192, "320": 320, "740": 740, "999": 740}
SONG_SEARCH_LIMIT = 10
SONG_COVER_MAX_BYTES = 5 * 1024 * 1024
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg")
SIREN_SITE_NAME = "塞壬唱片"
SIREN_SITE_SUFFIXES = (f"+{SIREN_SITE_NAME}", f"＋{SIREN_SITE_NAME}")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)


class SongApiError(RuntimeError):
    """A song upstream could not provide a usable response."""


class SongUnavailableError(SongApiError):
    """A song exists but has no usable download URL."""


@dataclass(frozen=True)
class SongServiceConfig:
    gd_api_url: str
    gd_site_url: str
    music_source: str
    default_bitrate: str
    cover_size: str
    siren_api_url: str
    siren_catalog_ttl_seconds: int


@dataclass(frozen=True)
class SongResult:
    song_id: str
    title: str
    artist: str
    album: str
    pic_id: str = ""
    source: str = "netease"


@dataclass(frozen=True)
class SongDownload:
    url: str
    size_mb: float | None = None
    bitrate: int = 0


@dataclass(frozen=True)
class SongSearchSession:
    results: tuple[SongResult, ...]
    created_at: float
    site: str = "gd"


_catalog_lock: asyncio.Lock | None = None
_catalog_cache: tuple[float, list[dict[str, Any]], dict[str, dict[str, Any]]] | None = None


def _headers(referer: str, *, accept: str = "*/*") -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": accept, "Referer": referer}


def _content_bytes(content: str | bytes | None) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    return b""


def _raise_for_status(status_code: int) -> None:
    if status_code == 429:
        raise SongApiError("请求过于频繁，请稍后再试。")
    if not 200 <= status_code < 300:
        raise SongApiError(f"音乐接口请求失败（HTTP {status_code}）")


async def fetch_json(driver: Any, url: str, *, referer: str, timeout: float = 30) -> Any:
    response = await driver.request(Request(
        "GET", url, headers=_headers(referer, accept="application/json"), timeout=timeout
    ))
    _raise_for_status(response.status_code)
    try:
        return json.loads(_content_bytes(response.content).decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SongApiError("音乐接口返回异常") from exc


async def fetch_limited_bytes(
    driver: Any,
    url: str,
    *,
    referer: str,
    max_bytes: int,
    expected_content_prefix: str | None = None,
    timeout: float = 30,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    seen_response = False
    async for response in driver.stream_request(
        Request("GET", url, headers=_headers(referer), timeout=timeout),
        chunk_size=64 * 1024,
    ):
        if not seen_response:
            seen_response = True
            _raise_for_status(response.status_code)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if expected_content_prefix and content_type and not content_type.startswith(expected_content_prefix):
                raise SongApiError("上游返回了错误的文件类型")
            try:
                content_length = int(response.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > max_bytes:
                raise SongApiError("上游文件超过大小限制")
        chunk = _content_bytes(response.content)
        total += len(chunk)
        if total > max_bytes:
            raise SongApiError("上游文件超过大小限制")
        chunks.append(chunk)
    if not seen_response:
        raise SongApiError("上游返回了空响应")
    return b"".join(chunks)


def join_song_artists(raw_artist: Any) -> str:
    if isinstance(raw_artist, (list, tuple)):
        artists = [str(item).strip() for item in raw_artist if str(item).strip()]
        return "、".join(artists) or "未知歌手"
    return str(raw_artist or "").strip() or "未知歌手"


def parse_song_search_response(payload: Any, default_source: str = "netease") -> list[SongResult]:
    if not isinstance(payload, list):
        raise ValueError("Invalid GD music search response")
    songs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        songs.append(SongResult(
            song_id=str(item["id"]),
            title=str(item["name"]).strip(),
            artist=join_song_artists(item.get("artist")),
            album=str(item.get("album") or "").strip(),
            pic_id=str(item.get("pic_id") or "").strip(),
            source=str(item.get("source") or default_source).strip(),
        ))
    return songs


async def search_songs(
    driver: Any, config: SongServiceConfig, name: str, limit: int = SONG_SEARCH_LIMIT
) -> list[SongResult]:
    payload = await fetch_json(
        driver,
        f"{config.gd_api_url}?types=search&source={config.music_source}"
        f"&name={quote_plus(name)}&count={limit}&pages=1",
        referer=f"{config.gd_site_url}/",
    )
    return parse_song_search_response(payload, config.music_source)[:limit]


def parse_song_size_mb(raw_size: Any) -> float | None:
    """GD documents KB, while current responses often use bytes."""
    try:
        size = float(raw_size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    if size <= 5_000_000:
        size *= 1024
    return round(size / (1024 * 1024), 1)


async def fetch_song_url(
    driver: Any,
    config: SongServiceConfig,
    track_id: str,
    bitrate: str,
    source: str,
) -> SongDownload:
    payload = await fetch_json(
        driver,
        f"{config.gd_api_url}?types=url&source={source}&id={quote_plus(track_id)}&br={bitrate}",
        referer=f"{config.gd_site_url}/",
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
    if quality in SONG_QUALITY_FLOOR_BPS:
        return SONG_QUALITY_FLOOR_BPS[quality]
    try:
        return max(int(quality), 0)
    except ValueError:
        return 0


async def fetch_song_download(
    driver: Any, config: SongServiceConfig, song: SongResult, quality: str
) -> SongDownload:
    try:
        download = await fetch_song_url(driver, config, song.song_id, quality, song.source)
    except SongUnavailableError:
        if quality == "320":
            raise
        try:
            await fetch_song_url(driver, config, song.song_id, "320", song.source)
        except SongApiError:
            raise SongUnavailableError("该歌曲暂时无法获取下载链接，可能无版权或已下架。") from None
        raise SongApiError(f"当前音源不支持所选音质（{quality}kbps），请降低音质后重试。") from None
    floor = song_quality_floor(quality)
    if floor and download.bitrate and download.bitrate < floor:
        raise SongApiError(
            f"当前音源不支持所选音质（{quality}kbps，实际仅支持 {download.bitrate}kbps），请降低音质后重试。"
        )
    return download


async def fetch_song_cover(
    driver: Any, config: SongServiceConfig, pic_id: str, source: str
) -> bytes | None:
    if not pic_id:
        return None
    try:
        payload = await fetch_json(
            driver,
            f"{config.gd_api_url}?types=pic&source={source}&id={quote_plus(pic_id)}&size={config.cover_size}",
            referer=f"{config.gd_site_url}/",
        )
        cover_url = str(payload.get("url") or "").strip() if isinstance(payload, dict) else ""
        if not cover_url:
            return None
        return await fetch_limited_bytes(
            driver,
            cover_url,
            referer=f"{config.gd_site_url}/",
            max_bytes=SONG_COVER_MAX_BYTES,
            expected_content_prefix="image/",
        )
    except Exception:
        LOGGER.warning("Failed to fetch song cover for pic_id=%s", pic_id, exc_info=True)
        return None


def parse_song_selection(argument: str) -> tuple[int, str | None] | None:
    match = re.fullmatch(r"(?:选择\s*)?(\d+)\s*(?:\+?\s*(\d+))?", argument.strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def split_site_suffix(argument: str) -> tuple[str, bool]:
    text = argument.strip()
    for marker in SIREN_SITE_SUFFIXES:
        if text.endswith(marker):
            return text[: -len(marker)].strip(), True
    return text, False


def parse_song_site_suffix(argument: str) -> tuple[str, str] | None:
    keyword, has_site = split_site_suffix(argument)
    if not has_site or not keyword or parse_song_selection(keyword) is not None:
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
    if download.bitrate <= 0 and song.source == "monster-siren":
        return "WAV 无损"
    return song_quality_label(download.bitrate)


def format_song_details(song: SongResult, download: SongDownload) -> str:
    album = song.album or "未知"
    size = f"（约 {download.size_mb:.1f} MB）" if download.size_mb else ""
    return (
        f"歌曲：{song.title}\n歌手：{song.artist}\n专辑：{album}\n"
        f"音质：{song_quality_display(song, download)}{size}"
    )


def detect_song_extension(path: Path, url: str = "") -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        header = b""
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
    ):
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


def join_siren_artists(raw_artist: Any) -> str:
    if isinstance(raw_artist, (list, tuple)):
        artists = [str(item).strip() for item in raw_artist if str(item).strip()]
        return "、".join(artists) or "塞壬唱片-MSR"
    return str(raw_artist or "").strip() or "塞壬唱片-MSR"


def parse_siren_songs(payload: Any, albums: dict[str, dict[str, Any]]) -> list[SongResult]:
    if not isinstance(payload, list):
        raise ValueError("Invalid siren songs response")
    songs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("cid") or not item.get("name"):
            continue
        album_cid = str(item.get("albumCid") or "")
        album = albums.get(album_cid) or {}
        songs.append(SongResult(
            song_id=str(item["cid"]),
            title=str(item["name"]).strip(),
            artist=join_siren_artists(item.get("artists")),
            album=str(album.get("name") or "").strip(),
            pic_id=album_cid,
            source="monster-siren",
        ))
    return songs


async def fetch_siren_catalog(
    driver: Any, config: SongServiceConfig
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    global _catalog_cache, _catalog_lock
    now = time.monotonic()
    if _catalog_cache is not None and now - _catalog_cache[0] < config.siren_catalog_ttl_seconds:
        return _catalog_cache[1], _catalog_cache[2]
    if _catalog_lock is None:
        _catalog_lock = asyncio.Lock()
    async with _catalog_lock:
        now = time.monotonic()
        if _catalog_cache is not None and now - _catalog_cache[0] < config.siren_catalog_ttl_seconds:
            return _catalog_cache[1], _catalog_cache[2]
        songs_payload, albums_payload = await asyncio.gather(
            fetch_json(driver, f"{config.siren_api_url}/api/songs", referer=f"{config.siren_api_url}/"),
            fetch_json(driver, f"{config.siren_api_url}/api/albums", referer=f"{config.siren_api_url}/"),
        )
        songs = songs_payload.get("data", {}).get("list", []) if isinstance(songs_payload, dict) else []
        albums_raw = albums_payload.get("data", []) if isinstance(albums_payload, dict) else []
        albums = {
            str(album.get("cid")): album
            for album in albums_raw
            if isinstance(album, dict) and album.get("cid")
        }
        valid_songs = [item for item in songs if isinstance(item, dict)]
        _catalog_cache = (now, valid_songs, albums)
        return valid_songs, albums


async def search_siren_songs(
    driver: Any, config: SongServiceConfig, name: str, limit: int = SONG_SEARCH_LIMIT
) -> list[SongResult]:
    keyword = name.strip().lower()
    if not keyword:
        return []
    songs, albums = await fetch_siren_catalog(driver, config)
    matched = [
        item for item in songs
        if keyword in str(item.get("name") or "").lower()
        or keyword in str(albums.get(str(item.get("albumCid") or ""), {}).get("name") or "").lower()
    ]
    return parse_siren_songs(matched, albums)[:limit]


async def fetch_siren_url(
    driver: Any, config: SongServiceConfig, song: SongResult
) -> SongDownload:
    payload = await fetch_json(
        driver,
        f"{config.siren_api_url}/api/song/{quote_plus(song.song_id)}",
        referer=f"{config.siren_api_url}/",
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    url = str(data.get("sourceUrl") or "").strip() if isinstance(data, dict) else ""
    if not url:
        raise SongUnavailableError("该歌曲暂时无法获取下载链接，可能无版权或已下架。")
    return SongDownload(url, None, 0)


async def fetch_siren_cover(
    driver: Any, config: SongServiceConfig, album_cid: str
) -> bytes | None:
    if not album_cid:
        return None
    try:
        _, albums = await fetch_siren_catalog(driver, config)
        cover_url = str((albums.get(album_cid) or {}).get("coverUrl") or "").strip()
        if not cover_url:
            return None
        return await fetch_limited_bytes(
            driver,
            cover_url,
            referer=f"{config.siren_api_url}/",
            max_bytes=SONG_COVER_MAX_BYTES,
            expected_content_prefix="image/",
        )
    except Exception:
        LOGGER.warning("Failed to fetch siren cover for album_cid=%s", album_cid, exc_info=True)
        return None


async def precheck_song_size(
    driver: Any, url: str, known_size_mb: float | None, *, referer: str
) -> float | None:
    if known_size_mb:
        return known_size_mb
    try:
        response = await driver.request(Request(
            "HEAD", url, headers=_headers(referer), timeout=20
        ))
        _raise_for_status(response.status_code)
        content_length = int(response.headers.get("Content-Length", "0") or 0)
        if content_length > 0:
            return round(content_length / (1024 * 1024), 1)
    except Exception:
        LOGGER.warning("Failed to precheck song size for %s", url[:80], exc_info=True)
    return None


async def download_song_source(
    driver: Any,
    url: str,
    output_path: Path,
    *,
    max_bytes: int,
    referer: str,
) -> None:
    total = 0
    seen_response = False
    with output_path.open("wb") as output:
        async for response in driver.stream_request(
            Request("GET", url, headers=_headers(referer), timeout=60),
            chunk_size=1024 * 1024,
        ):
            if not seen_response:
                seen_response = True
                _raise_for_status(response.status_code)
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise SongApiError("下载链接未返回音频文件")
                content_length = int(response.headers.get("Content-Length", "0") or 0)
                if content_length > max_bytes:
                    raise SongApiError("歌曲文件超过下载大小限制")
            chunk = _content_bytes(response.content)
            total += len(chunk)
            if total > max_bytes:
                raise SongApiError("歌曲文件超过下载大小限制")
            output.write(chunk)
    if not seen_response or total == 0:
        raise SongApiError("下载链接返回了空文件")


def reset_state() -> None:
    global _catalog_cache, _catalog_lock
    _catalog_cache = None
    _catalog_lock = None
