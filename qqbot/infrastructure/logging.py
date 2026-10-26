"""Keep operational logs while hiding unrelated OneBot traffic."""
import re
import sys
from typing import Any, Mapping

import nonebot.log as nonebot_log
from nonebot.log import default_filter, default_format, logger


_ONEBOT_EVENT = re.compile(r"^OneBot V11 (?P<self_id>\d+) \| \[(?P<event>[^]]+)]")
_AUTO_EMOJI_MATCHER_MESSAGES = (
    "Event will be handled by Matcher",
    "running complete",
)


def should_display_log(level_name: str, message: str) -> bool:
    """Return whether an already formatted NoneBot message is useful to operators."""
    if level_name in {"WARNING", "ERROR", "CRITICAL"}:
        return True
    if (
        level_name == "INFO"
        and "module=plugins.auto_emoji" in message
        and any(marker in message for marker in _AUTO_EMOJI_MATCHER_MESSAGES)
    ):
        return False
    if level_name != "SUCCESS":
        return True

    match = _ONEBOT_EVENT.match(message)
    if not match:
        return True
    if not match.group("event").startswith("message."):
        return False
    return f"[at:qq={match.group('self_id')}]" in message


def bot_log_filter(record: Mapping[str, Any]) -> bool:
    """Apply NoneBot's configured log level, then the bot traffic filter."""
    return default_filter(record) and should_display_log(
        record["level"].name,
        str(record["message"]),
    )


def install_bot_log_filter() -> None:
    """Replace only NoneBot's default stdout sink with the filtered equivalent."""
    try:
        logger.remove(nonebot_log.logger_id)
    except ValueError:
        pass
    nonebot_log.logger_id = logger.add(
        sys.stdout,
        level=0,
        diagnose=False,
        filter=bot_log_filter,
        format=default_format,
    )
