from typing import Any

from plugin_control import default_enabled_plugin_ids, normalize_enabled_plugin_ids

from .common import CommandContext


HELP_LINES_BY_PLUGIN: dict[str, tuple[str, ...]] = {
    "basic": (
        "/help - 查看指令列表",
        "/hello - 回复 啦啦啦",
    ),
    "rps": (
        "/猜拳 @成员 - 与成员猜拳，输家禁言 5 分钟",
    ),
    "daily_wife": (
        "/今日群友 - 随机抽取今日群友老婆",
        "/结芬 - 向今日群友老婆发起结芬请求",
        "/愿意 或 /不愿意 - 回复结芬请求",
    ),
    "courtship": (
        "/求偶 @成员 - 向成员发起求偶请求",
        "/接受求偶 或 /拒绝求偶 - 回复求偶请求",
    ),
    "song": (
        "/song 歌曲名 - 搜索歌曲并列出候选",
        "/song 序号 - 下载所选歌曲",
    ),
    "novel": (
        "/novel 小说名 - 查询轻小说目录链接",
        "/第1章 或 /第1卷 - 返回上次查询小说的对应链接",
    ),
    "summary": (
        "/总结 条数n - 调用 DeepSeek 总结最近 n 条群聊消息，最多 500 条",
    ),
}


def format_help(enabled_plugin_ids: tuple[str, ...] | None = None) -> str:
    source_plugin_ids = default_enabled_plugin_ids() if enabled_plugin_ids is None else enabled_plugin_ids
    plugin_ids = normalize_enabled_plugin_ids(source_plugin_ids)
    lines = ["可用指令："]
    for plugin_id in plugin_ids:
        lines.extend(HELP_LINES_BY_PLUGIN.get(plugin_id, ()))
    return "\n".join(lines)


def dispatch_command(command: str, enabled_plugin_ids: tuple[str, ...] | None = None) -> str | None:
    command_name = command.split(maxsplit=1)[0] if command else ""
    if command_name == "/help":
        return format_help(enabled_plugin_ids)
    if command_name == "/hello":
        return "啦啦啦"
    return None


class BasicCommandPlugin:
    def matches(self, command_name: str, context: CommandContext) -> bool:
        return command_name in {"/help", "/hello"}

    async def handle(self, bot: Any, websocket: Any, event: dict[str, Any], context: CommandContext) -> None:
        enabled_plugin_ids = getattr(bot, "enabled_plugin_ids", None)
        reply = dispatch_command(context.text, enabled_plugin_ids)
        if reply:
            await bot._send_reply(websocket, event, reply)
