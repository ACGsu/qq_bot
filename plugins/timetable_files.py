"""NapCat group-file normalization and bounded ICS transport (no course parsing)."""

import hashlib
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


MAX_ICS_BYTES = 10 * 1024 * 1024
DOWNLOAD_SOCKET_TIMEOUT = 10
DOWNLOAD_DEADLINE_SECONDS = 30
CQ_FILE_RE = re.compile(r"\[CQ:file,([^\]]*)\]")


class TimetableFileError(ValueError):
    """An error whose message is safe to send to the uploading member."""


@dataclass(frozen=True)
class GroupFileUpload:
    group_id: str
    user_id: str
    file_id: str
    name: str
    size: int | None


@dataclass(frozen=True)
class CalendarPayload:
    content: bytes
    sha256: str


def group_session_key(event: dict[str, Any]) -> tuple[str, str] | None:
    values = (event.get("group_id"), event.get("user_id"))
    # Only event identities are used; neither @targets nor calendar fields own a session.
    if any(isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() for value in values):
        return None
    if any(len(str(value)) > 20 or int(value) <= 0 for value in values):
        return None
    return str(int(values[0])), str(int(values[1]))


def _cq_unescape(value: str) -> str:
    # Decode ampersands last, so escaped CQ-looking text is never decoded twice.
    return value.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def extract_group_files(event: dict[str, Any]) -> list[GroupFileUpload]:
    key = group_session_key(event)
    if key is None:
        return []
    records: list[dict[str, Any]] = []
    if event.get("post_type") == "notice" and event.get("notice_type") == "group_upload":
        if isinstance(event.get("file"), dict):
            records.append(event["file"])
    elif event.get("post_type") == "message" and event.get("message_type") == "group":
        message = event.get("message")
        if isinstance(message, list):
            records = [
                segment["data"] for segment in message
                if isinstance(segment, dict) and segment.get("type") == "file"
                and isinstance(segment.get("data"), dict)
            ]
        elif isinstance(message, str):
            for match in CQ_FILE_RE.finditer(message):
                fields = {}
                for field in match.group(1).split(","):
                    name, separator, value = field.partition("=")
                    if separator:
                        fields[name] = _cq_unescape(value)
                records.append(fields)

    uploads = []
    for data in records:
        file_id = data.get("file_id") or data.get("id")
        name = data.get("name") or data.get("file_name") or data.get("file") or ""
        if not isinstance(file_id, str) or not file_id or len(file_id) > 2048:
            continue
        if not isinstance(name, str):
            name = ""
        size = data.get("file_size", data.get("size"))
        try:
            size = int(size) if size is not None and not isinstance(size, bool) else None
        except (TypeError, ValueError, OverflowError):
            size = None
        uploads.append(GroupFileUpload(key[0], key[1], file_id, name, size))
    return uploads


def validate_download_url(url: Any) -> str:
    # URLs are requested from NapCat, never taken from a file message or the ICS body.
    if not isinstance(url, str) or not url or len(url) > 16384 or any(ord(char) <= 32 for char in url):
        raise TimetableFileError("NapCat 未返回有效的文件下载地址，请重新上传 .ics 文件。")
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
        parts.port  # Validate malformed ports without exposing the URL in errors.
    except ValueError:
        valid = False
    if not valid:
        raise TimetableFileError("文件下载地址不安全或无效，仅支持 HTTP(S) 下载。")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def inspect_calendar_payload(content: bytes, max_bytes: int = MAX_ICS_BYTES) -> CalendarPayload:
    """Check size/encoding/calendar envelope, NOT VEVENTs or recurrence semantics."""
    if not content:
        raise TimetableFileError("课表文件为空，请上传有效的 .ics 文件。")
    if len(content) > max_bytes:
        raise TimetableFileError(f"课表文件超过 {max_bytes / (1024 * 1024):g} MiB 上限，请缩小后重新上传。")
    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise TimetableFileError("文件不是 UTF-8 编码的 ICS，请重新导出 .ics 文件；不支持 Excel。") from None
    if (
        "\x00" in text
        or not re.match(r"\ABEGIN:VCALENDAR(?:\r\n|\r|\n)", text, re.IGNORECASE)
        or not re.search(r"(?:\r\n|\r|\n)END:VCALENDAR\Z", text, re.IGNORECASE)
    ):
        raise TimetableFileError("文件缺少有效的 VCALENDAR 外壳，请上传 .ics 文件，不能将 Excel 改名后上传。")
    return CalendarPayload(content, hashlib.sha256(content).hexdigest())


def download_calendar_file(url: str, max_bytes: int = MAX_ICS_BYTES) -> CalendarPayload:
    """Blocking, bounded download; the plugin must run this via asyncio.to_thread."""
    url = validate_download_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": "qq-bot-timetable/1",
        "Accept": "text/calendar, */*;q=0.1",
        "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    try:
        with opener.open(request, timeout=DOWNLOAD_SOCKET_TIMEOUT) as response:
            validate_download_url(response.geturl())
            declared_length = response.headers.get("Content-Length")
            try:
                declared_length = int(declared_length) if declared_length is not None else None
            except (ValueError, TypeError):
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                raise TimetableFileError("文件实际大小超过接收上限，请缩小后重新上传。")
            content = bytearray()
            # read1 lets us re-check the total deadline even for trickling responses.
            while True:
                if time.monotonic() >= deadline:
                    raise TimetableFileError("课表文件下载超时，请稍后重新上传。")
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise TimetableFileError("文件实际大小超过接收上限，请缩小后重新上传。")
            if declared_length is not None and len(content) != declared_length:
                raise TimetableFileError("文件下载不完整，请稍后重新上传。")
        return inspect_calendar_payload(bytes(content), max_bytes)
    except TimetableFileError:
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        # Exception strings may contain signed URLs. Never forward or log them.
        raise TimetableFileError("课表文件下载失败或超时，请稍后重新上传。") from None
