import logging
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

from .common import CommandContext


LOGGER = logging.getLogger("qq-bot")
SUMMARY_COMMAND = "/总结"
SUMMARY_DEFAULT_MESSAGE_LIMIT = 100
SUMMARY_MESSAGE_LIMIT = 500
SUMMARY_BATCH_SIZE = 100
DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
SUMMARY_USAGE = "用法：@bot /总结 条数n，例如 @bot /总结 100；最多 500 条。"

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{1,}|[0-9]{2,}|[\u4e00-\u9fff]{2,}")
STOP_WORDS = {
    "这个",
    "那个",
    "就是",
    "还是",
    "然后",
    "感觉",
    "可以",
    "没有",
    "不是",
    "什么",
    "怎么",
    "现在",
    "今天",
    "一下",
    "一个",
    "已经",
    "这么",
    "这么",
    "你们",
    "我们",
    "他们",
    "哈哈",
    "哈哈哈",
    "表情",
    "图片",
    "视频",
    "语音",
    "总结",
}


class GroupSummaryPlugin:
    plugin_id = "summary"

    def __init__(self, settings: dict[str, str] | None = None, deepseek_client: Any | None = None) -> None:
        self.settings = settings or {}
        self.deepseek_client = deepseek_client

    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name == SUMMARY_COMMAND

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        if event.get("message_type") != "group":
            await bot._send_reply(websocket, event, "总结只能在群聊中使用。")
            return

        limit = parse_summary_limit(context.text)
        if limit is None:
            await bot._send_reply(websocket, event, SUMMARY_USAGE)
            return

        group_id = str(event.get("group_id"))
        await bot._send_reply(websocket, event, f"正在读取最近 {limit} 条群聊记录并调用 DeepSeek 总结，请稍等。")
        try:
            LOGGER.info("Start summarizing group %s history", group_id)
            messages = await fetch_group_history(bot, websocket, group_id, limit)
        except Exception:
            LOGGER.exception("Failed to fetch group message history")
            await bot._send_reply(websocket, event, "获取群聊记录失败，请稍后再试。")
            return

        transcript = format_group_messages_for_deepseek(messages, limit)
        if not transcript:
            await bot._send_reply(websocket, event, "群聊上下文总结：没有获取到可总结的文本消息。")
            return

        try:
            client = self.deepseek_client or DeepSeekApiClient.from_settings(self.settings)
            LOGGER.info("Fetched %d messages for group %s summary; calling DeepSeek", len(messages), group_id)
            summary = await run_deepseek_summary(client, transcript, min(limit, len(messages)))
        except DeepSeekConfigurationError as exc:
            LOGGER.warning("DeepSeek summary is not configured: %s", exc)
            await bot._send_reply(websocket, event, f"DeepSeek API 未配置：{exc}")
            return
        except DeepSeekApiError as exc:
            LOGGER.warning("DeepSeek summary request failed: %s", exc)
            await bot._send_reply(websocket, event, f"调用 DeepSeek 总结失败：{exc}")
            return
        except Exception:
            LOGGER.exception("Failed to summarize group history with DeepSeek")
            await bot._send_reply(websocket, event, "调用 DeepSeek 总结失败，请稍后再试。")
            return

        await bot._send_reply(websocket, event, summary)


def parse_summary_limit(command_text: str) -> int | None:
    argument = command_text.split(maxsplit=1)[1].strip() if len(command_text.split(maxsplit=1)) > 1 else ""
    if not argument:
        return SUMMARY_DEFAULT_MESSAGE_LIMIT

    match = re.fullmatch(r"(?:条数\s*)?(\d{1,4})", argument)
    if not match:
        return None

    value = int(match.group(1))
    if value <= 0:
        return None
    return min(value, SUMMARY_MESSAGE_LIMIT)


async def run_deepseek_summary(client: Any, transcript: str, requested_count: int) -> str:
    import asyncio
    import inspect

    summarize = client.summarize_group_chat
    if inspect.iscoroutinefunction(summarize):
        return await summarize(transcript, requested_count)
    result = await asyncio.to_thread(summarize, transcript, requested_count)
    if inspect.isawaitable(result):
        return await result
    return result


class DeepSeekConfigurationError(RuntimeError):
    pass


class DeepSeekApiError(RuntimeError):
    pass


class DeepSeekApiClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEEPSEEK_API_BASE_URL,
        model: str = DEEPSEEK_DEFAULT_MODEL,
        timeout: float = 90,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or DEEPSEEK_DEFAULT_MODEL
        self.timeout = timeout
        if not self.api_key:
            raise DeepSeekConfigurationError("请在插件配置中填写 DeepSeek API Key。")

    @classmethod
    def from_settings(cls, settings: dict[str, str]) -> "DeepSeekApiClient":
        api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
        base_url = settings.get("deepseek_api_base_url") or os.getenv("DEEPSEEK_API_BASE_URL", DEEPSEEK_API_BASE_URL)
        model = settings.get("deepseek_model") or os.getenv("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL)
        return cls(api_key=api_key, base_url=base_url, model=model)

    def summarize_group_chat(self, transcript: str, requested_count: int) -> str:
        prompt = build_deepseek_prompt(transcript, requested_count)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个QQ群聊上下文总结助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        body = self.post_json(f"{self.base_url}/chat/completions", payload)
        reply = extract_deepseek_api_reply(body)
        if not reply:
            raise DeepSeekApiError("DeepSeek API 没有返回总结内容")
        return reply

    def post_json(self, url: str, payload: dict[str, Any]) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers=self.headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            message = extract_api_error_message(detail) or detail
            raise DeepSeekApiError(f"HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise DeepSeekApiError(f"请求失败：{exc.reason}") from exc

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "qq-bot/1.0",
        }


def build_deepseek_prompt(transcript: str, requested_count: int) -> str:
    return (
        "请用中文总结下面QQ群聊上下文。\n"
        f"范围：最近 {requested_count} 条消息中的可读文本。\n"
        "要求：\n"
        "1. 先用 2-4 句概括大家主要聊了什么。\n"
        "2. 用要点列出关键话题、结论、待办或争议。\n"
        "3. 不要编造未出现的信息；不要输出与总结无关的寒暄。\n\n"
        "群聊记录：\n"
        f"{transcript}"
    )


def format_group_messages_for_deepseek(messages: list[dict[str, Any]], limit: int = SUMMARY_MESSAGE_LIMIT) -> str:
    ordered = sorted(messages[:limit], key=message_time)
    lines: list[str] = []
    for message in ordered:
        text = compact_text(message_to_text(message))
        if not text or is_summary_command_text(text):
            continue
        sender = message_sender_name(message)
        lines.append(f"{sender}: {clip_text(text, 500)}")
    return "\n".join(lines)


def is_summary_command_text(text: str) -> bool:
    return bool(re.search(r"(?:^|\s)/总结(?:\s|$)", text))


def parse_json_body(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeepSeekApiError("DeepSeek API 返回了非 JSON 响应") from exc


def extract_deepseek_api_reply(body: str) -> str:
    payload = parse_json_body(body)
    if not isinstance(payload, dict):
        raise DeepSeekApiError("DeepSeek API 返回格式不正确")

    error_message = extract_api_error_message(payload)
    if error_message:
        raise DeepSeekApiError(error_message)

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    text = first_choice.get("text")
    return text.strip() if isinstance(text, str) else ""


def extract_api_error_message(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ""
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        for key in ("message", "msg"):
            message = value.get(key)
            if isinstance(message, str) and message:
                return message
    return ""


async def fetch_group_history(bot: Any, websocket: Any, group_id: str, limit: int = SUMMARY_MESSAGE_LIMIT) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    next_seq: int | None = None

    while len(messages) < limit:
        params: dict[str, Any] = {
            "group_id": int(group_id),
            "count": min(SUMMARY_BATCH_SIZE, limit - len(messages)),
        }
        if next_seq is not None:
            params["message_seq"] = next_seq

        try:
            response = await bot._send_action_request(websocket, "get_group_msg_history", params, timeout=20)
            batch = extract_history_messages(response)
        except Exception:
            if messages:
                LOGGER.exception("Stop fetching group history after %d messages", len(messages))
                break
            raise
        if not batch:
            break

        added = 0
        for message in batch:
            key = message_key(message)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            messages.append(message)
            added += 1
            if len(messages) >= limit:
                break

        seqs = [seq for seq in (message_sequence(message) for message in batch) if seq is not None]
        if not seqs or added == 0:
            break
        # QQ/NapCat message sequences can have gaps; request from the oldest real seq and dedupe overlaps.
        next_seq = min(seqs)

    return messages[:limit]


def extract_history_messages(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("messages", "message", "items", "list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def message_key(message: dict[str, Any]) -> str:
    for key in ("message_id", "real_id", "message_seq", "seq"):
        value = message.get(key)
        if value is not None:
            return f"{key}:{value}"
    return f"object:{id(message)}"


def message_sequence(message: dict[str, Any]) -> int | None:
    for key in ("message_seq", "seq", "message_id", "real_id"):
        value = message.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def message_sender_name(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        for key in ("card", "nickname", "user_id"):
            value = str(sender.get(key) or "").strip()
            if value:
                return value
    return str(message.get("user_id") or "未知成员")


def message_to_text(message: dict[str, Any]) -> str:
    raw = message.get("raw_message")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    content = message.get("message")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for segment in content:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment_type == "text":
            parts.append(str(data.get("text") or ""))
        elif segment_type == "at":
            qq = str(data.get("qq") or "").strip()
            parts.append(f"@{qq}" if qq else "@成员")
        elif segment_type == "image":
            parts.append("[图片]")
        elif segment_type == "record":
            parts.append("[语音]")
        elif segment_type == "video":
            parts.append("[视频]")
        elif segment_type == "face":
            parts.append("[表情]")
    return " ".join(part.strip() for part in parts if part.strip()).strip()


def message_time(message: dict[str, Any]) -> int:
    try:
        return int(message.get("time") or 0)
    except (TypeError, ValueError):
        return 0


def summarize_group_messages(messages: list[dict[str, Any]], limit: int = SUMMARY_MESSAGE_LIMIT) -> str:
    ordered = sorted(messages[:limit], key=message_time)
    readable: list[tuple[str, str]] = []
    participant_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()

    for message in ordered:
        sender = message_sender_name(message)
        participant_counts[sender] += 1
        text = compact_text(message_to_text(message))
        if not text or text.startswith(SUMMARY_COMMAND):
            continue
        readable.append((sender, text))
        keyword_counts.update(extract_keywords(text))

    message_count = len(ordered)
    text_count = len(readable)
    if message_count == 0:
        return "群聊上下文总结：没有获取到可总结的群聊记录。"

    top_keywords = [word for word, _count in keyword_counts.most_common(8)]
    top_speakers = [f"{name} {count}条" for name, count in participant_counts.most_common(5)]
    highlights = representative_messages(readable, keyword_counts, 6)

    lines = [
        f"群聊上下文总结（最近 {message_count} 条，文本 {text_count} 条）",
        f"主要话题：{format_list(top_keywords) if top_keywords else '文本信息较少，主要是图片、表情或短消息。'}",
        f"活跃成员：{format_list(top_speakers) if top_speakers else '暂无'}",
        "聊了什么：",
    ]

    if top_keywords:
        lines.append(f"- 大家集中提到了 {format_list(top_keywords[:5])}。")
    if text_count < message_count:
        lines.append(f"- 其中有 {message_count - text_count} 条主要是非文本内容或空消息。")
    if not top_keywords and text_count:
        lines.append("- 文本内容比较零散，没有形成特别集中的关键词。")

    if highlights:
        lines.append("代表性上文：")
        for sender, text in highlights:
            lines.append(f"- {sender}：{clip_text(text, 70)}")

    return "\n".join(lines)


def extract_keywords(text: str) -> list[str]:
    words: list[str] = []
    for match in TOKEN_RE.findall(text):
        word = match.strip().lower()
        if len(word) < 2 or word in STOP_WORDS or word.startswith("["):
            continue
        if len(word) > 14:
            word = word[:14]
        words.append(word)
    return words


def representative_messages(
    messages: list[tuple[str, str]],
    keyword_counts: Counter[str],
    limit: int,
) -> list[tuple[str, str]]:
    scored: list[tuple[int, int, str, str]] = []
    for index, (sender, text) in enumerate(messages):
        if len(text) < 6 or text.startswith(SUMMARY_COMMAND):
            continue
        score = sum(keyword_counts.get(word, 0) for word in extract_keywords(text))
        score += min(len(text), 80) // 20
        scored.append((score, index, sender, text))

    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _score, _index, sender, text in sorted(scored, reverse=True):
        normalized = clip_text(text, 30)
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append((sender, text))
        if len(selected) >= limit:
            break
    return list(reversed(selected))


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clip_text(text: str, limit: int) -> str:
    text = compact_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_list(items: list[str]) -> str:
    return "、".join(items)
