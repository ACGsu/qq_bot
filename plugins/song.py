import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote_plus, urlencode, urlsplit
from uuid import uuid4

from .common import CommandContext, NOT_FOUND_IMAGE_PATH, at_segment, command_argument, image_segment_from_file, text_segment


LOGGER = logging.getLogger("qq-bot")
XIAGEBA_BASE_URL = os.getenv("XIAGEBA_BASE_URL", "https://xiageba.liumingye.cn").rstrip("/")
SONG_DOWNLOAD_DIR = Path(__file__).resolve().parents[1] / "downloads"
SONG_MAX_DOWNLOAD_BYTES = int(os.getenv("SONG_MAX_DOWNLOAD_MB", "80")) * 1024 * 1024
SONG_SEARCH_LIMIT = 10
SONG_SESSION_SECONDS = int(os.getenv("SONG_SESSION_MINUTES", "10")) * 60


@dataclass(frozen=True)
class SongResult:
    song_id: str
    title: str
    artist: str
    album: str
    cover_url: str


@dataclass(frozen=True)
class SongDownload:
    quality: str
    url: str


@dataclass(frozen=True)
class SongDetails:
    song_id: str
    title: str
    artist: str
    album: str
    cover_url: str
    play_url: str
    downloads: tuple[SongDownload, ...]


@dataclass(frozen=True)
class SongSearchSession:
    results: tuple[SongResult, ...]
    created_at: float


class QuarkDownloadError(RuntimeError):
    pass


def fetch_json(url: str, timeout: float = 30) -> Any:
    req = request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json",
            "Referer": f"{XIAGEBA_BASE_URL}/",
        },
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def parse_song_search_response(payload: Any) -> list[SongResult]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Invalid Xiageba search response")
    songs = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            continue
        songs.append(
            SongResult(
                song_id=str(item["id"]),
                title=str(item["title"]).strip(),
                artist=str(item.get("artist") or "未知歌手").strip(),
                album=str(item.get("album") or "").strip(),
                cover_url=str(item.get("cover") or "").strip(),
            )
        )
    return songs


def search_songs(name: str, limit: int = SONG_SEARCH_LIMIT) -> list[SongResult]:
    query = quote_plus(name)
    payload = fetch_json(f"{XIAGEBA_BASE_URL}/api/music/search?q={query}&page=1&pageSize={limit}")
    return parse_song_search_response(payload)[:limit]


def parse_song_details_response(payload: Any) -> SongDetails:
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("title"):
        raise ValueError("Invalid Xiageba song details response")
    raw_downloads = payload.get("downloads") or []
    if isinstance(raw_downloads, dict):
        raw_downloads = [raw_downloads]
    downloads = tuple(
        SongDownload(str(item.get("quality") or "未知音质"), str(item.get("url") or ""))
        for item in raw_downloads
        if isinstance(item, dict) and item.get("url")
    )
    return SongDetails(
        song_id=str(payload["id"]),
        title=str(payload["title"]).strip(),
        artist=str(payload.get("artist") or "未知歌手").strip(),
        album=str(payload.get("album") or "").strip(),
        cover_url=str(payload.get("cover") or "").strip(),
        play_url=str(payload.get("playUrl") or "").strip(),
        downloads=downloads,
    )


def fetch_song_details(song_id: str) -> SongDetails:
    return parse_song_details_response(fetch_json(f"{XIAGEBA_BASE_URL}/api/music/{quote_plus(song_id)}"))


def parse_song_selection(argument: str) -> int | None:
    match = re.fullmatch(r"(?:选择\s*)?(\d+)", argument.strip())
    return int(match.group(1)) if match else None


def format_song_search_results(results: list[SongResult]) -> str:
    lines = ["搜索到以下歌曲："]
    for index, song in enumerate(results, 1):
        album = f"，专辑：{song.album}" if song.album else ""
        lines.append(f"{index}. {song.title} - {song.artist}{album}")
    lines.append("请回复：@bot /song 序号（例如：@bot /song 2）")
    return "\n".join(lines)


def is_share_page_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"pan.quark.cn", "pan.baidu.com", "www.aliyundrive.com", "www.alipan.com"}


def is_quark_share_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == "pan.quark.cn"


def choose_song_source(song: SongDetails, allow_quark: bool = False) -> SongDownload | None:
    direct_downloads = [download for download in song.downloads if not is_share_page_url(download.url)]
    mp3_downloads = [download for download in direct_downloads if "mp3" in download.quality.lower()]
    if mp3_downloads:
        return mp3_downloads[0]
    if allow_quark:
        quark_mp3 = [
            download
            for download in song.downloads
            if is_quark_share_url(download.url) and "mp3" in download.quality.lower()
        ]
        if quark_mp3:
            return quark_mp3[0]
    if song.play_url and not is_share_page_url(song.play_url):
        return SongDownload("播放音频", song.play_url)
    if direct_downloads:
        return direct_downloads[0]
    if allow_quark:
        return next((download for download in song.downloads if is_quark_share_url(download.url)), None)
    return None


def format_song_details(song: SongDetails) -> str:
    album = song.album or "未知"
    qualities = "、".join(download.quality for download in song.downloads) or "未知"
    return f"歌曲：{song.title}\n歌手：{song.artist}\n专辑：{album}\n可用音质：{qualities}"


def download_song_source(
    url: str,
    output_path: Path,
    max_bytes: int = SONG_MAX_DOWNLOAD_BYTES,
    cookie: str | None = None,
) -> None:
    """Stream a remote song to disk while enforcing a size limit."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": f"{XIAGEBA_BASE_URL}/",
    }
    if cookie:
        headers["Cookie"] = cookie
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


QUARK_API_BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
QUARK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/146.0.0.0 Safari/537.36 QuarkPC/7.0.0"
)


def quark_api_request(
    cookie: str,
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = {"pr": "ucpro", "fr": "pc", **(params or {})}
    url = f"{QUARK_API_BASE_URL}/{path.lstrip('/')}?{urlencode(query)}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "User-Agent": QUARK_USER_AGENT,
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
    except error.HTTPError as exc:
        raise QuarkDownloadError(f"夸克接口请求失败（HTTP {exc.code}）") from exc
    if not isinstance(result, dict) or result.get("status") != 200:
        code = result.get("code", "unknown") if isinstance(result, dict) else "invalid"
        message = result.get("message", "接口返回异常") if isinstance(result, dict) else "接口返回异常"
        raise QuarkDownloadError(f"夸克接口失败（{code}）：{message}")
    return result


def quark_share_id(share_url: str) -> str:
    match = re.search(r"https?://pan\.quark\.cn/s/([A-Za-z0-9]+)", share_url)
    if not match:
        raise QuarkDownloadError("无法解析夸克分享链接")
    return match.group(1)


def quark_share_files(cookie: str, pwd_id: str, stoken: str, pdir_fid: str = "0") -> list[dict[str, Any]]:
    result = quark_api_request(
        cookie,
        "share/sharepage/detail",
        params={
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": pdir_fid,
            "force": 0,
            "_page": 1,
            "_size": 100,
            "_fetch_total": 1,
            "_sort": "file_type:asc,file_name:asc",
        },
    )
    items = result.get("data", {}).get("list", [])
    return [item for item in items if isinstance(item, dict)]


def choose_quark_audio_file(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    files = [item for item in items if not item.get("dir")]
    audio_extensions = (".mp3", ".flac", ".aac", ".m4a", ".wav", ".ogg")
    mp3_files = [item for item in files if str(item.get("file_name", "")).lower().endswith(".mp3")]
    if mp3_files:
        return mp3_files[0]
    return next(
        (item for item in files if str(item.get("file_name", "")).lower().endswith(audio_extensions)),
        None,
    )


def wait_for_quark_task(cookie: str, task_id: str, timeout: int = 60) -> dict[str, Any]:
    deadline = time.time() + timeout
    retry_index = 0
    while time.time() < deadline:
        result = quark_api_request(cookie, "task", params={"task_id": task_id, "retry_index": retry_index})
        task_data = result.get("data", {})
        if task_data.get("status") == 2:
            return task_data
        if task_data.get("status") == 3:
            raise QuarkDownloadError("夸克临时转存任务失败")
        retry_index += 1
        time.sleep(1)
    raise QuarkDownloadError("夸克临时转存任务超时")


def download_quark_share_source(
    share_url: str,
    cookie: str,
    output_path: Path,
    max_bytes: int = SONG_MAX_DOWNLOAD_BYTES,
) -> None:
    pwd_id = quark_share_id(share_url)
    folder_fid: str | None = None
    try:
        token_result = quark_api_request(
            cookie,
            "share/sharepage/token",
            method="POST",
            payload={"pwd_id": pwd_id, "passcode": "", "support_visit_limit_private_share": True},
        )
        stoken = str(token_result.get("data", {}).get("stoken") or "")
        if not stoken:
            raise QuarkDownloadError("夸克分享令牌为空，分享可能已失效")

        shared_file = choose_quark_audio_file(quark_share_files(cookie, pwd_id, stoken))
        if not shared_file:
            raise QuarkDownloadError("夸克分享中没有可下载的音频文件")

        folder_result = quark_api_request(
            cookie,
            "file",
            method="POST",
            payload={"file_name": f"qqbot-song-{uuid4().hex}", "pdir_fid": "0", "dir_init_lock": False},
        )
        folder_fid = str(folder_result.get("data", {}).get("fid") or "")
        if not folder_fid:
            raise QuarkDownloadError("无法创建夸克临时目录")

        fid = str(shared_file.get("fid") or "")
        fid_token = str(shared_file.get("share_fid_token") or shared_file.get("fid_token") or "")
        save_result = quark_api_request(
            cookie,
            "share/sharepage/save",
            method="POST",
            payload={
                "fid_list": [fid],
                "fid_token_list": [fid_token] if fid_token else [],
                "to_pdir_fid": folder_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "pdir_save_all": False,
                "exclude_fids": [],
                "scene": "link",
            },
        )
        task_id = str(save_result.get("data", {}).get("task_id") or "")
        if task_id:
            wait_for_quark_task(cookie, task_id)

        stored_result = quark_api_request(
            cookie,
            "file/sort",
            params={"pdir_fid": folder_fid, "_page": 1, "_size": 20, "_fetch_total": 1},
        )
        stored_items = stored_result.get("data", {}).get("list", [])
        stored_file = choose_quark_audio_file([item for item in stored_items if isinstance(item, dict)])
        if not stored_file:
            raise QuarkDownloadError("夸克临时目录中没有找到转存后的音频")

        download_result = quark_api_request(
            cookie,
            "file/download",
            method="POST",
            payload={"fids": [str(stored_file.get("fid"))]},
        )
        download_items = download_result.get("data", [])
        if isinstance(download_items, dict):
            download_items = [download_items]
        download_url = str(download_items[0].get("download_url") or "") if download_items else ""
        if not download_url:
            raise QuarkDownloadError("夸克没有返回文件下载地址")
        download_song_source(download_url, output_path, max_bytes=max_bytes, cookie=cookie)
    finally:
        if folder_fid:
            try:
                quark_api_request(
                    cookie,
                    "file/delete",
                    method="POST",
                    payload={"filelist": [folder_fid], "action_type": 2, "exclude_fids": []},
                )
            except Exception:
                LOGGER.warning("Failed to clean temporary Quark folder")


def convert_song_to_mp3(source_path: Path, output_path: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(source_path), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(output_path)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        error_text = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg failed: {error_text}")


def safe_song_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{(cleaned or 'song')[:80]}.mp3"


class SongPlugin:
    def __init__(self) -> None:
        self.song_sessions: dict[str, SongSearchSession] = {}

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == "/song"

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        argument = command_argument(context.text)
        if not argument:
            await bot._send_reply(websocket, event, "用法：@bot /song 歌曲名称；搜索后使用 @bot /song 序号 选择歌曲。")
            return

        session_key = self._song_session_key(event)
        if argument.lower() in {"取消", "cancel"}:
            self.song_sessions.pop(session_key, None)
            await bot._send_reply(websocket, event, "已取消本次歌曲选择。")
            return

        selection = parse_song_selection(argument)
        if selection is not None:
            await self._handle_song_selection(bot, websocket, event, session_key, selection)
            return

        try:
            songs = await asyncio.to_thread(search_songs, argument)
        except Exception:
            LOGGER.exception("Song search failed")
            await self._send_song_not_found(bot, websocket, event)
            return

        if not songs:
            await self._send_song_not_found(bot, websocket, event)
            return

        self.song_sessions[session_key] = SongSearchSession(tuple(songs), time.time())
        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_search_results(songs)))
        await bot._send_reply(websocket, event, message)

    async def _handle_song_selection(
        self,
        bot: Any,
        websocket: Any,
        event: dict[str, Any],
        session_key: str,
        selection: int,
    ) -> None:
        session = self.song_sessions.get(session_key)
        if not session or time.time() - session.created_at > SONG_SESSION_SECONDS:
            self.song_sessions.pop(session_key, None)
            await bot._send_reply(websocket, event, "歌曲候选已过期，请重新输入：@bot /song 歌曲名称")
            return
        if selection < 1 or selection > len(session.results):
            await bot._send_reply(websocket, event, f"请选择 1 到 {len(session.results)} 之间的序号。")
            return

        selected = session.results[selection - 1]
        self.song_sessions.pop(session_key, None)
        try:
            song = await asyncio.to_thread(fetch_song_details, selected.song_id)
        except Exception:
            LOGGER.exception("Song details request failed")
            await bot._send_reply(websocket, event, "歌曲详情获取失败，请稍后重新搜索。")
            return

        user_id = str(event.get("user_id"))
        message: list[dict[str, Any]] = []
        if event.get("message_type") == "group":
            message.extend((at_segment(user_id), text_segment(" ")))
        message.append(text_segment(format_song_details(song)))
        await bot._send_reply(websocket, event, message)

        source = choose_song_source(song, allow_quark=bool(bot.config.quark_cookie))
        if not source:
            links = "\n".join(f"{download.quality}：{download.url}" for download in song.downloads)
            if any(is_quark_share_url(download.url) for download in song.downloads):
                explanation = "该站当前仅提供夸克分享页，但 bot 尚未配置 QUARK_COOKIE。"
            else:
                explanation = "该站当前仅提供暂不支持自动下载的网盘分享页。"
            await bot._send_reply(websocket, event, f"{explanation}\n{links}" if links else explanation)
            return

        await bot._send_reply(websocket, event, f"正在下载并转换为 MP3：{source.quality}")
        file_path = await self._download_song_file(bot, source.url)
        if not file_path:
            await bot._send_reply(websocket, event, "歌曲转换或下载失败，请稍后再试。")
            return
        try:
            await self._send_song_file(bot, websocket, event, file_path, f"{song.title} - {song.artist}")
        finally:
            file_path.unlink(missing_ok=True)

    def _song_session_key(self, event: dict[str, Any]) -> str:
        scope = event.get("group_id") if event.get("message_type") == "group" else "private"
        return f"{scope}:{event.get('user_id')}"

    async def _download_song_file(self, bot: Any, audio_url: str) -> Path | None:
        SONG_DOWNLOAD_DIR.mkdir(exist_ok=True)
        identifier = uuid4().hex
        source_path = SONG_DOWNLOAD_DIR / f"{identifier}.source"
        output_path = SONG_DOWNLOAD_DIR / f"{identifier}.mp3"
        try:
            if is_quark_share_url(audio_url):
                if not bot.config.quark_cookie:
                    raise QuarkDownloadError("QUARK_COOKIE 未配置")
                await asyncio.to_thread(download_quark_share_source, audio_url, bot.config.quark_cookie, source_path)
            else:
                await asyncio.to_thread(download_song_source, audio_url, source_path)
            await asyncio.to_thread(convert_song_to_mp3, source_path, output_path)
            return output_path
        except Exception:
            LOGGER.exception("Failed to download or convert song file")
            output_path.unlink(missing_ok=True)
            return None
        finally:
            source_path.unlink(missing_ok=True)

    async def _send_song_file(self, bot: Any, websocket: Any, event: dict[str, Any], file_path: Path, title: str) -> None:
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
        params: dict[str, Any] = {
            "file": f"base64://{encoded}",
            "name": safe_song_filename(title),
        }
        if event.get("message_type") == "group":
            params["group_id"] = int(event["group_id"])
            action = "upload_group_file"
        else:
            params["user_id"] = int(event["user_id"])
            action = "upload_private_file"
        await bot._send_action(websocket, action, params)

    async def _send_song_not_found(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        message = [text_segment("私密马赛，没有找到喵~")]
        if NOT_FOUND_IMAGE_PATH.exists():
            message.append(image_segment_from_file(NOT_FOUND_IMAGE_PATH))
        await bot._send_reply(websocket, event, message)
