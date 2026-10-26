import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
NOT_FOUND_IMAGE_PATH = ASSETS_DIR / "not_found.jpg"
COMMAND_NOT_FOUND_IMAGE_PATH = ASSETS_DIR / "command_not_found.jpg"
CHINA_TZ = timezone(timedelta(hours=8))


def command_argument(command: str) -> str:
    parts = command.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def at_segment(qq: str) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": qq}}


def qq_avatar_segment(qq: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"}}


class OneBotActionError(RuntimeError):
    """OneBot explicitly rejected an action, unlike an unknown transport outcome."""


def image_segment_from_bytes(content: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(content).decode("ascii")
    return {"type": "image", "data": {"file": f"base64://{encoded}"}}


def image_segment_from_file(path: Path) -> dict[str, Any]:
    return image_segment_from_bytes(path.read_bytes())


def today_date_key() -> str:
    return datetime.now(CHINA_TZ).date().isoformat()
