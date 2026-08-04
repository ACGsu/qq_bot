import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Protocol


CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^\],]+)[^\]]*\]")
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
NOT_FOUND_IMAGE_PATH = ASSETS_DIR / "not_found.jpg"
COMMAND_NOT_FOUND_IMAGE_PATH = ASSETS_DIR / "command_not_found.jpg"
CHINA_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class CommandContext:
    text: str
    mentions: list[str]


def _same_qq(left: Any, right: Any) -> bool:
    return str(left) == str(right)


def extract_command_after_mention(message: Any, bot_qq: str | None) -> str | None:
    """Return command text after @bot, or None when the message did not mention this bot."""
    context = extract_command_context_after_mention(message, bot_qq)
    return context.text if context else None


def extract_command_context_after_mention(message: Any, bot_qq: str | None) -> CommandContext | None:
    if isinstance(message, list):
        return _extract_context_from_segments(message, bot_qq)
    if isinstance(message, str):
        return _extract_context_from_cq_text(message, bot_qq)
    return None


def _extract_context_from_segments(segments: list[dict[str, Any]], bot_qq: str | None) -> CommandContext | None:
    for index, segment in enumerate(segments):
        if segment.get("type") != "at":
            continue

        qq = segment.get("data", {}).get("qq")
        if bot_qq and not _same_qq(qq, bot_qq):
            continue
        if not bot_qq and str(qq).lower() == "all":
            continue

        parts: list[str] = []
        mentions: list[str] = []
        for rest in segments[index + 1 :]:
            if rest.get("type") == "text":
                parts.append(str(rest.get("data", {}).get("text", "")))
            elif rest.get("type") == "at":
                mentioned_qq = str(rest.get("data", {}).get("qq", ""))
                if mentioned_qq and mentioned_qq.lower() != "all":
                    mentions.append(mentioned_qq)
        return CommandContext("".join(parts).strip(), mentions)
    return None


def _extract_context_from_cq_text(message: str, bot_qq: str | None) -> CommandContext | None:
    for match in CQ_AT_RE.finditer(message):
        qq = match.group(1)
        if bot_qq and not _same_qq(qq, bot_qq):
            continue
        if not bot_qq and qq.lower() == "all":
            continue
        rest = message[match.end() :].strip()
        mentions = [item for item in CQ_AT_RE.findall(rest) if item.lower() != "all"]
        text = CQ_AT_RE.sub("", rest).strip()
        return CommandContext(text, mentions)
    return None


def command_argument(command: str) -> str:
    parts = command.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def at_segment(qq: str) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": qq}}


def qq_avatar_segment(qq: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"}}


def image_segment_from_file(path: Path) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "data": {"file": f"base64://{encoded}"}}


def today_date_key() -> str:
    return datetime.now(CHINA_TZ).date().isoformat()


class BotPlugin(Protocol):
    def matches(self, command_name: str, context: CommandContext) -> bool:
        ...

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        ...
