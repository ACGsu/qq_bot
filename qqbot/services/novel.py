"""Linovelib lookup and parsing through NoneBot's HTTP client driver."""
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

from nonebot.drivers import Request

LOGGER = logging.getLogger("qq-bot")
LINOVELIB_BASE_URL = "https://www.linovelib.com"
LINOVELIB_TW_BASE_URL = "https://tw.linovelib.com"
BING_RSS_URL = "https://www.bing.com/search"
LINOVELIB_BASE_URLS = (LINOVELIB_BASE_URL, LINOVELIB_TW_BASE_URL)
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


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


async def fetch_text(driver: Any, url: str, *, data: bytes | None = None, timeout: float = 20) -> str:
    headers = dict(REQUEST_HEADERS)
    parsed_url = urlsplit(url)
    headers["Referer"] = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    method = "POST" if data is not None else "GET"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    response = await driver.request(Request(method, url, headers=headers, content=data, timeout=timeout))
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"novel upstream returned HTTP {response.status_code}")
    content = response.content
    if isinstance(content, bytes):
        return content.decode("utf-8", "replace")
    if isinstance(content, str):
        return content
    raise RuntimeError("novel upstream returned an invalid body")


def normalize_title(text: str) -> str:
    return re.sub(r"[\s　「」『』《》()（）!！?？.。,，:：、·~～—_-]+", "", text).lower()


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


def extract_novel_id(url: str) -> str | None:
    match = re.search(r"/novel/(\d+)(?:\.html|/catalog)?/?(?:[?#].*)?$", url)
    return match.group(1) if match else None


def _base_url_for(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return LINOVELIB_BASE_URL


def _novel_info(novel_id: str, title: str, base_url: str = LINOVELIB_BASE_URL) -> NovelInfo:
    return NovelInfo(
        title,
        f"{base_url}/novel/{novel_id}.html",
        f"{base_url}/novel/{novel_id}/catalog",
    )


def _clean_page_title(title: str) -> str:
    title = strip_tags(title)
    title = re.split(r"(?:小说在线阅读|小說線上看|在线阅读|線上看)", title, maxsplit=1)[0]
    title = re.split(r"\s+-\s+linovelib\.com|_", title, maxsplit=1, flags=re.I)[0]
    return title.strip()


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
            title = _clean_page_title(page_title.group(1))
    novel_id = extract_novel_id(url)
    if not title or not novel_id:
        return None
    return _novel_info(novel_id, title, _base_url_for(url))


def parse_novel_links(html: str, base_url: str = LINOVELIB_BASE_URL) -> list[NovelInfo]:
    novels: dict[str, NovelInfo] = {}
    for match in re.finditer(r'href="([^"]*/novel/(\d+)\.html)"[^>]*>(.*?)</a>', html, re.S):
        href, novel_id, raw_title = match.groups()
        title = strip_tags(raw_title)
        if not title or title in {"立即阅读", "开始阅读"}:
            continue
        resolved_url = urljoin(base_url, href)
        novels[novel_id] = _novel_info(novel_id, title, _base_url_for(resolved_url))
    return list(novels.values())


def parse_bing_rss(html: str) -> list[NovelInfo]:
    try:
        root = ET.fromstring(html)
    except ET.ParseError:
        return []
    novels: dict[str, NovelInfo] = {}
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        match = re.search(
            r"https://(?P<host>www|tw)\.linovelib\.com/novel/(?P<id>\d+)"
            r"(?:\.html|/catalog|/[^/?#]+\.html)",
            link,
        )
        if not match:
            continue
        novel_id = match.group("id")
        if novel_id in novels:
            continue
        title = _clean_page_title(item.findtext("title") or "") or f"小说 {novel_id}"
        novels[novel_id] = _novel_info(
            novel_id,
            title,
            f"https://{match.group('host')}.linovelib.com",
        )
    return list(novels.values())


def _search_queries(name: str) -> list[str]:
    queries = [name.strip()]
    relaxed = queries[0]
    relaxed = re.sub(r"^关于我", "", relaxed)
    relaxed = re.sub(r"转生(?:后)?(?:变成|成为|成)", "转生", relaxed)
    relaxed = re.sub(r"(?:这|那)(?:档|件)事$", "", relaxed)
    if relaxed and relaxed not in queries:
        queries.append(relaxed)
    return queries


def _is_subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for char in haystack:
        if position < len(needle) and char == needle[position]:
            position += 1
    return bool(needle) and position == len(needle)


def _select_candidate(name: str, candidates: list[NovelInfo], *, trust_first: bool = False) -> NovelInfo | None:
    normalized = normalize_title(name)
    exact = [novel for novel in candidates if normalize_title(novel.title) == normalized]
    if exact:
        return exact[0]
    partial = [novel for novel in candidates if normalized in normalize_title(novel.title)]
    if partial:
        return partial[0]
    subsequence = [novel for novel in candidates if _is_subsequence(normalized, normalize_title(novel.title))]
    if subsequence:
        return subsequence[0]
    return candidates[0] if trust_first and candidates else None


async def find_best_novel(driver: Any, name: str) -> NovelInfo | None:
    direct_id = extract_novel_id(name.strip())
    if direct_id:
        return _novel_info(direct_id, f"小说 {direct_id}")

    try:
        data = f"searchkey={quote_plus(name)}".encode("ascii")
        candidates = parse_novel_links(
            await fetch_text(driver, f"{LINOVELIB_BASE_URL}/S6/", data=data)
        )
    except Exception:
        candidates = []
        LOGGER.info("Linovelib search endpoint unavailable; falling back to indexed search")
    selected = _select_candidate(name, candidates)
    if selected:
        return selected

    for query in _search_queries(name):
        try:
            encoded_query = quote_plus(f"site:linovelib.com {query}")
            url = f"{BING_RSS_URL}?format=rss&q={encoded_query}"
            candidates = parse_bing_rss(await fetch_text(driver, url))
        except Exception:
            LOGGER.info("Indexed Linovelib search unavailable for query %s", query)
            continue
        selected = _select_candidate(query, candidates, trust_first=True)
        if selected:
            return selected

    try:
        candidates = parse_novel_links(await fetch_text(driver, LINOVELIB_BASE_URL))
    except Exception:
        return None
    return _select_candidate(name, candidates)


def parse_novel_catalog_html(novel: NovelInfo, html: str) -> NovelCatalog:
    page_title = re.search(r"<title>(.*?)</title>", html, re.S)
    if page_title:
        catalog_title = _clean_page_title(page_title.group(1))
        if catalog_title and not catalog_title.startswith("出现错误"):
            novel = NovelInfo(catalog_title, novel.url, novel.catalog_url)

    volumes: list[NovelVolume] = []
    chapters: list[NovelChapter] = []
    base_url = _base_url_for(novel.catalog_url)

    desktop_starts = [
        match.start()
        for match in re.finditer(r'<div\s+class=["\']volume clearfix["\'][^>]*>', html, re.I)
    ]
    desktop_blocks = [
        html[start:end]
        for start, end in zip(desktop_starts, [*desktop_starts[1:], len(html)])
    ]
    mobile_starts = [
        match.start()
        for match in re.finditer(
            r'<li[^>]*class=["\'][^"\']*\bchapter-bar\b[^"\']*["\'][^>]*>',
            html,
            re.I,
        )
    ]
    mobile_blocks = [
        html[start:end]
        for start, end in zip(mobile_starts, [*mobile_starts[1:], len(html)])
    ]

    for block in desktop_blocks or mobile_blocks:
        volume_match = re.search(
            r'<h[23][^>]*>.*?<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?</h[23]>',
            block,
            re.S | re.I,
        )
        if not volume_match:
            volume_match = re.search(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*<h3[^>]*>(.*?)</h3>\s*</a>',
                block,
                re.S | re.I,
            )
        if not volume_match:
            continue
        volume = NovelVolume(
            len(volumes) + 1,
            strip_tags(volume_match.group(2)),
            urljoin(base_url, volume_match.group(1)),
        )
        volumes.append(volume)
        for chapter_match in re.finditer(
            r'<li[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*</li>',
            block,
            re.S | re.I,
        ):
            chapter_url = urljoin(base_url, chapter_match.group(1))
            if "/vol_" in chapter_url:
                continue
            chapter_title = strip_tags(chapter_match.group(2))
            if not chapter_title:
                continue
            chapters.append(NovelChapter(
                len(chapters) + 1,
                chapter_title,
                chapter_url,
                volume.index,
                volume.title,
            ))
    return NovelCatalog(novel, volumes, chapters)


async def lookup_catalog(driver: Any, name: str) -> NovelCatalog | None:
    novel = await find_best_novel(driver, name)
    if novel is None:
        return None
    novel_id = extract_novel_id(novel.catalog_url)
    if novel_id is None:
        return None
    preferred_base = _base_url_for(novel.catalog_url)
    base_urls = [preferred_base, *(base for base in LINOVELIB_BASE_URLS if base != preferred_base)]
    last_error: Exception | None = None
    for base_url in base_urls:
        candidate = _novel_info(novel_id, novel.title, base_url)
        try:
            catalog = parse_novel_catalog_html(
                candidate,
                await fetch_text(driver, candidate.catalog_url),
            )
        except Exception as error:
            last_error = error
            continue
        if catalog.volumes or catalog.chapters:
            return catalog
        last_error = RuntimeError("novel upstream returned an empty catalog")
    if last_error is not None:
        raise last_error
    return None


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
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
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
