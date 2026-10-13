import json
import logging
import os
import threading
import time
from typing import Any
from urllib import error, request
from urllib.parse import quote_plus

from .song import SONG_SEARCH_LIMIT, SongApiError, SongDownload, SongResult, SongUnavailableError


LOGGER = logging.getLogger("qq-bot")
SIREN_API_BASE_URL = os.getenv("SIREN_API_BASE_URL", "https://monster-siren.hypergryph.com").rstrip("/")
SIREN_SITE_URL = SIREN_API_BASE_URL
SIREN_CATALOG_TTL_SECONDS = int(os.getenv("SIREN_CATALOG_TTL_MINUTES", "10")) * 60
SIREN_COVER_MAX_BYTES = 5 * 1024 * 1024
SIREN_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

_catalog_lock = threading.Lock()
_catalog_cache: tuple[float, list[dict[str, Any]], dict[str, dict[str, Any]]] | None = None


def _fetch_json(url: str, timeout: float = 30) -> Any:
    req = request.Request(
        url,
        headers={
            "User-Agent": SIREN_USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{SIREN_SITE_URL}/",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except error.HTTPError as exc:
        if exc.code == 429:
            raise SongApiError("请求过于频繁，请稍后再试。") from exc
        raise SongApiError(f"音乐接口请求失败（HTTP {exc.code}）") from exc


def fetch_siren_catalog() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """拉取 /api/songs + /api/albums，带 TTL 缓存与线程锁，返回 (歌曲列表, 专辑映射)。"""
    global _catalog_cache
    with _catalog_lock:
        now = time.monotonic()
        if _catalog_cache is not None and now - _catalog_cache[0] < SIREN_CATALOG_TTL_SECONDS:
            return _catalog_cache[1], _catalog_cache[2]
        songs_payload = _fetch_json(f"{SIREN_API_BASE_URL}/api/songs")
        albums_payload = _fetch_json(f"{SIREN_API_BASE_URL}/api/albums")
        songs = songs_payload.get("data", {}).get("list", []) if isinstance(songs_payload, dict) else []
        albums_raw = albums_payload.get("data", []) if isinstance(albums_payload, dict) else []
        albums = {
            str(album.get("cid")): album
            for album in albums_raw
            if isinstance(album, dict) and album.get("cid")
        }
        songs = [song for song in songs if isinstance(song, dict)]
        _catalog_cache = (now, songs, albums)
        return songs, albums


def join_siren_artists(raw_artist: Any) -> str:
    if isinstance(raw_artist, (list, tuple)):
        artists = [str(item).strip() for item in raw_artist if str(item).strip()]
        return "、".join(artists) or "塞壬唱片-MSR"
    return str(raw_artist or "").strip() or "塞壬唱片-MSR"


def parse_siren_songs(payload: Any, albums: dict[str, dict[str, Any]]) -> list[SongResult]:
    """把 /api/songs 列表映射为统一 SongResult，专辑名取自专辑映射。"""
    if not isinstance(payload, list):
        raise ValueError("Invalid siren songs response")
    songs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("cid") or not item.get("name"):
            continue
        album_cid = str(item.get("albumCid") or "")
        album = albums.get(album_cid) or {}
        songs.append(
            SongResult(
                song_id=str(item["cid"]),
                title=str(item["name"]).strip(),
                artist=join_siren_artists(item.get("artists")),
                album=str(album.get("name") or "").strip(),
                pic_id=album_cid,
                source="monster-siren",
            )
        )
    return songs


def search_siren_songs(name: str, limit: int = SONG_SEARCH_LIMIT) -> list[SongResult]:
    """在缓存目录内按 曲名/专辑名 子串匹配（大小写不敏感），返回前 limit 条。"""
    keyword = name.strip().lower()
    if not keyword:
        return []
    songs, albums = fetch_siren_catalog()
    matched = [
        song
        for song in songs
        if keyword in str(song.get("name") or "").lower()
        or keyword in str(albums.get(str(song.get("albumCid") or ""), {}).get("name") or "").lower()
    ]
    return parse_siren_songs(matched, albums)[:limit]


def fetch_siren_url(song: SongResult) -> SongDownload:
    """请求 /api/song/{cid} 取 WAV 直链；WAV 无码率/大小字段，bitrate=0、size_mb=None。"""
    payload = _fetch_json(f"{SIREN_API_BASE_URL}/api/song/{quote_plus(song.song_id)}")
    if not isinstance(payload, dict):
        raise SongUnavailableError("音乐接口返回异常")
    url = str(payload.get("data", {}).get("sourceUrl") or "").strip() if isinstance(payload.get("data"), dict) else ""
    if not url:
        raise SongUnavailableError("该歌曲暂时无法获取下载链接，可能无版权或已下架。")
    return SongDownload(url, None, 0)


def fetch_siren_cover(album_cid: str) -> bytes | None:
    """获取专辑封面图片字节；任何失败都返回 None，不阻断主流程。"""
    if not album_cid:
        return None
    try:
        _, albums = fetch_siren_catalog()
        album = albums.get(album_cid) or {}
        cover_url = str(album.get("coverUrl") or "").strip()
        if not cover_url:
            return None
        req = request.Request(
            cover_url,
            headers={"User-Agent": SIREN_USER_AGENT, "Referer": f"{SIREN_SITE_URL}/"},
        )
        with request.urlopen(req, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/"):
                return None
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > SIREN_COVER_MAX_BYTES:
                return None
            data = response.read(SIREN_COVER_MAX_BYTES + 1)
        if len(data) > SIREN_COVER_MAX_BYTES:
            return None
        return data
    except Exception:
        LOGGER.warning("Failed to fetch siren cover for album_cid=%s", album_cid, exc_info=True)
        return None
