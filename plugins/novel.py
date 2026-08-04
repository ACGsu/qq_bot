import asyncio
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib import request
from urllib.parse import quote_plus, urljoin

from .common import CommandContext, NOT_FOUND_IMAGE_PATH, command_argument, image_segment_from_file, text_segment


LOGGER = logging.getLogger("qq-bot")
LINOVELIB_BASE_URL = "https://www.linovelib.com"


@dataclass(frozen=True)
class NovelInfo:
    title: str
    url: str
    catalog_url: str


@dataclass(frozen=True)
class NovelVolume:
    index: int
    title: str
    url: str


@dataclass(frozen=True)
class NovelChapter:
    index: int
    title: str
    url: str
    volume_index: int
    volume_title: str


@dataclass(frozen=True)
class NovelCatalog:
    novel: NovelInfo
    volumes: list[NovelVolume]
    chapters: list[NovelChapter]


def fetch_text(url: str, timeout: float = 20, data: bytes | None = None) -> str:
    req = request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


def extract_novel_id(url: str) -> str | None:
    match = re.search(r"/novel/(\d+)(?:\.html|/catalog)?$", url)
    return match.group(1) if match else None


def parse_novel_info_from_page(url: str, html: str) -> NovelInfo | None:
    title = ""
    title_match = re.search(r'<meta\s+property="og:novel:book_name"\s+content="([^"]+)"', html)
    if title_match:
        title = unescape(title_match.group(1)).strip()
    if not title:
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if h1_match:
            title = strip_tags(h1_match.group(1))
    if not title:
        page_title = re.search(r"<title>(.*?)</title>", html, re.S)
        if page_title:
            title = re.sub(r"在线阅读.*$", "", strip_tags(page_title.group(1))).strip()

    novel_id = extract_novel_id(url)
    if not title or not novel_id:
        return None
    novel_url = f"{LINOVELIB_BASE_URL}/novel/{novel_id}.html"
    return NovelInfo(title=title, url=novel_url, catalog_url=f"{LINOVELIB_BASE_URL}/novel/{novel_id}/catalog")


def parse_novel_links(html: str) -> list[NovelInfo]:
    novels: dict[str, NovelInfo] = {}
    for match in re.finditer(r'href="([^"]*/novel/(\d+)\.html)"[^>]*>(.*?)</a>', html, re.S):
        href = match.group(1)
        novel_id = match.group(2)
        title = strip_tags(match.group(3))
        if not title or title in {"立即阅读", "开始阅读"}:
            continue
        url = urljoin(LINOVELIB_BASE_URL, href)
        novels[novel_id] = NovelInfo(title=title, url=url, catalog_url=f"{LINOVELIB_BASE_URL}/novel/{novel_id}/catalog")
    return list(novels.values())


def find_best_novel(name: str) -> NovelInfo | None:
    direct_id = extract_novel_id(name.strip())
    if direct_id:
        return NovelInfo(
            title=f"小说 {direct_id}",
            url=f"{LINOVELIB_BASE_URL}/novel/{direct_id}.html",
            catalog_url=f"{LINOVELIB_BASE_URL}/novel/{direct_id}/catalog",
        )

    search_html = ""
    try:
        data = f"searchkey={quote_plus(name)}".encode("ascii")
        search_html = fetch_text(f"{LINOVELIB_BASE_URL}/S6/", data=data)
    except Exception:
        LOGGER.info("Linovelib search endpoint unavailable; falling back to homepage links")

    candidates = parse_novel_links(search_html) if search_html else []
    if not candidates:
        candidates = parse_novel_links(fetch_text(LINOVELIB_BASE_URL))

    normalized = normalize_title(name)
    exact = [novel for novel in candidates if normalize_title(novel.title) == normalized]
    if exact:
        return exact[0]

    partial = [novel for novel in candidates if normalized in normalize_title(novel.title)]
    return partial[0] if partial else None


def parse_novel_catalog(novel: NovelInfo) -> NovelCatalog:
    html = fetch_text(novel.catalog_url)
    page_title = re.search(r"<title>(.*?)</title>", html, re.S)
    if page_title:
        catalog_title = re.sub(r"在线阅读.*$", "", strip_tags(page_title.group(1))).strip()
        if catalog_title and not catalog_title.startswith("出现错误"):
            novel = NovelInfo(catalog_title, novel.url, novel.catalog_url)

    volumes: list[NovelVolume] = []
    chapters: list[NovelChapter] = []

    volume_starts = [match.start() for match in re.finditer(r'<div class="volume clearfix">', html)]
    volume_blocks = [html[start:end] for start, end in zip(volume_starts, [*volume_starts[1:], len(html)])]

    for volume_index, block in enumerate(volume_blocks, start=1):
        vol_match = re.search(r'<h2[^>]*>.*?href="([^"]+)".*?>(.*?)</a>.*?</h2>', block, re.S)
        if not vol_match:
            continue
        volume = NovelVolume(volume_index, strip_tags(vol_match.group(2)), urljoin(LINOVELIB_BASE_URL, vol_match.group(1)))
        volumes.append(volume)

        for chapter_match in re.finditer(r'<li[^>]*>\s*<a href="([^"]+)">(.*?)</a>\s*</li>', block, re.S):
            chapter_url = urljoin(LINOVELIB_BASE_URL, chapter_match.group(1))
            if "/vol_" in chapter_url:
                continue
            chapters.append(
                NovelChapter(
                    index=len(chapters) + 1,
                    title=strip_tags(chapter_match.group(2)),
                    url=chapter_url,
                    volume_index=volume.index,
                    volume_title=volume.title,
                )
            )

    return NovelCatalog(novel=novel, volumes=volumes, chapters=chapters)


def parse_ordinal_command(command: str) -> tuple[str, int] | None:
    text = command.strip().lstrip("/")
    match = re.fullmatch(r"第([0-9一二三四五六七八九十百两]+)(章|卷)", text)
    if not match:
        return None
    number = parse_chinese_number(match.group(1))
    if number is None or number <= 0:
        return None
    return match.group(2), number


def parse_chinese_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if "百" in text:
        left, _, right = text.partition("百")
        hundred = digits.get(left, 1 if left == "" else None)
        if hundred is None:
            return None
        rest = parse_chinese_number(right) if right else 0
        return hundred * 100 + (rest or 0)
    if "十" in text:
        left, _, right = text.partition("十")
        ten = digits.get(left, 1 if left == "" else None)
        one = digits.get(right, 0 if right == "" else None)
        if ten is None or one is None:
            return None
        return ten * 10 + one
    total = 0
    for char in text:
        if char not in digits:
            return None
        total = total * 10 + digits[char]
    return total


class NovelPlugin:
    def __init__(self) -> None:
        self.novel_sessions: dict[str, NovelCatalog] = {}

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == "/novel" or parse_ordinal_command(command_name) is not None

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        command_name = context.text.split(maxsplit=1)[0] if context.text else ""
        if command_name == "/novel":
            await self._handle_novel_command(bot, websocket, event, context)
            return
        await self._handle_novel_link_command(bot, websocket, event, context)

    async def _handle_novel_command(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        name = command_argument(context.text)
        if not name:
            await bot._send_reply(websocket, event, "用法：@bot /novel 小说名字")
            return

        try:
            novel = await asyncio.to_thread(find_best_novel, name)
            if not novel:
                await self._send_novel_not_found(bot, websocket, event)
                return
            catalog = await asyncio.to_thread(parse_novel_catalog, novel)
        except Exception:
            LOGGER.exception("Linovelib novel lookup failed")
            await self._send_novel_not_found(bot, websocket, event)
            return

        self.novel_sessions[str(event.get("user_id"))] = catalog
        await bot._send_reply(
            websocket,
            event,
            (
                f"已找到：{catalog.novel.title}\n"
                f"目录：{catalog.novel.catalog_url}\n"
                f"共 {len(catalog.volumes)} 卷，{len(catalog.chapters)} 章。\n"
                "接下来可输入：@bot /第1卷 或 @bot /第1章"
            ),
        )

    async def _handle_novel_link_command(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        parsed = parse_ordinal_command(context.text.split(maxsplit=1)[0])
        if not parsed:
            return
        catalog = self.novel_sessions.get(str(event.get("user_id")))
        if not catalog:
            await bot._send_reply(websocket, event, "请先输入：@bot /novel 小说名字")
            return

        kind, index = parsed
        if kind == "卷":
            if index > len(catalog.volumes):
                await bot._send_reply(websocket, event, f"这本书暂时没有第{index}卷。")
                return
            volume = catalog.volumes[index - 1]
            await bot._send_reply(websocket, event, f"{catalog.novel.title}\n第{index}卷：{volume.title}\n{volume.url}")
            return

        if index > len(catalog.chapters):
            await bot._send_reply(websocket, event, f"这本书暂时没有第{index}章。")
            return
        chapter = catalog.chapters[index - 1]
        await bot._send_reply(
            websocket,
            event,
            f"{catalog.novel.title}\n第{index}章：{chapter.title}\n所属卷：{chapter.volume_title}\n{chapter.url}",
        )

    async def _send_novel_not_found(self, bot: Any, websocket: Any, event: dict[str, Any]) -> None:
        message = [text_segment("私密马赛，暂时没有该小说呢")]
        if NOT_FOUND_IMAGE_PATH.exists():
            message.append(image_segment_from_file(NOT_FOUND_IMAGE_PATH))
        await bot._send_reply(websocket, event, message)
