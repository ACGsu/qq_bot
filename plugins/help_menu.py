"""Shared, configuration-aware help content for text and QQ forward cards."""

from dataclasses import dataclass
from typing import Any

from plugin_control import normalize_enabled_plugin_ids, parse_bool_setting

from .common import text_segment


HELP_LINES_BY_PLUGIN: dict[str, tuple[str, ...]] = {
    "basic": (
        "/help - 群内查看可点击的功能导航，私聊查看文字帮助",
        "/help 文本 或 /help 全部 - 查看全部文字帮助",
        "/help 分类名 - 查看指定分类的文字帮助",
        "/hello - 回复 啦啦啦",
    ),
    "rps": (
        "/猜拳 @成员 - 与成员猜拳，输家禁言 1 分钟",
    ),
    "daily_wife": (
        "/今日群友 或 /今日老婆 - 随机抽取今日群友老婆",
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
    "timetable": (
        "/课表 - 查看课表功能说明（仅支持 ICS）",
        "/导入课表 - 开启本人在当前群的 ICS 文件接收，不支持 Excel",
        "/已导入 - 解析 ICS 并正式入库，更新会覆盖旧课表",
        "/更新课表 - 为已导入成员开启更新，上传新 ICS 后再次 /已导入",
        "/课ing - 图片展示仍在本群的已导入成员当前课程（头像、群名片/昵称、时间）",
        "/今日课程 - @发送者，以图片只显示他自己的今日课表；绘图失败改用文字",
        "/取消导入 - 取消接收并清除本次暂存，不删除已入库课表",
    ),
    "summary": (
        "/总结 条数n - 调用 DeepSeek 总结最近 n 条群聊消息，最多 500 条",
    ),
}

MARRIAGE_HELP_LINES = (
    "/结芬 - 向今日群友老婆发起结芬请求",
    "/愿意 或 /不愿意 - 回复结芬请求",
)

HELP_TITLES_BY_PLUGIN = {
    "basic": "基础指令",
    "rps": "猜拳",
    "daily_wife": "今日老婆",
    "courtship": "求偶",
    "song": "歌曲下载",
    "novel": "小说链接",
    "timetable": "课表",
    "summary": "群聊总结",
}

HELP_ALIASES_BY_PLUGIN = {
    "basic": ("基础", "帮助"),
    "daily_wife": ("今日群友",),
    "song": ("歌曲", "点歌"),
    "novel": ("小说",),
    "summary": ("总结",),
}

TEXT_HELP_TOPICS = frozenset({"文本", "全部", "text", "all"})


@dataclass(frozen=True)
class HelpSection:
    plugin_id: str
    title: str
    lines: tuple[str, ...]


def get_help_sections(
    enabled_plugin_ids: tuple[str, ...] | None = None,
    plugin_settings: dict[str, dict[str, str]] | None = None,
) -> tuple[HelpSection, ...]:
    daily_wife_settings = (plugin_settings or {}).get("daily_wife", {})
    marriage_enabled = parse_bool_setting(daily_wife_settings.get("marriage_enabled"), default=True)
    timetable_settings = (plugin_settings or {}).get("timetable", {})
    group_isolation_enabled = parse_bool_setting(timetable_settings.get("group_isolation_enabled"), default=True)
    sections = []
    for plugin_id in normalize_enabled_plugin_ids(enabled_plugin_ids):
        lines = HELP_LINES_BY_PLUGIN.get(plugin_id, ())
        if not lines:
            continue
        if plugin_id == "daily_wife" and marriage_enabled:
            lines += MARRIAGE_HELP_LINES
        if plugin_id == "timetable":
            lines += ((
                "群聊隔离已开启：各群分别导入；可在图形控制台的课表设置中关闭。"
                if group_isolation_enabled else
                "群聊隔离已关闭：同一 QQ 跨群共享最近更新的一份课表；/课ing 仍只展示当前群成员，结果在群内可见。"
            ),)
        sections.append(HelpSection(plugin_id, HELP_TITLES_BY_PLUGIN[plugin_id], lines))
    return tuple(sections)


def format_help(
    enabled_plugin_ids: tuple[str, ...] | None = None,
    plugin_settings: dict[str, dict[str, str]] | None = None,
) -> str:
    lines = ["可用指令："]
    for section in get_help_sections(enabled_plugin_ids, plugin_settings):
        lines.extend(section.lines)
    return "\n".join(lines)


def format_help_topic(
    topic: str,
    enabled_plugin_ids: tuple[str, ...] | None = None,
    plugin_settings: dict[str, dict[str, str]] | None = None,
) -> str:
    normalized = topic.strip().removeprefix("/").casefold()
    if not normalized or normalized in TEXT_HELP_TOPICS:
        return format_help(enabled_plugin_ids, plugin_settings)

    sections = get_help_sections(enabled_plugin_ids, plugin_settings)
    for section in sections:
        aliases = (section.plugin_id, section.title, *HELP_ALIASES_BY_PLUGIN.get(section.plugin_id, ()))
        if normalized in aliases:
            return (
                f"{section.title}｜指令说明\n" + "\n".join(section.lines)
                + "\n\n请在聊天中真正 @bot 后输入指令。"
                + "\n@bot /help 返回帮助首页；@bot /help 文本 查看全部文字帮助。"
            )

    available = "、".join(section.title for section in sections) or "暂无可用分类"
    return (
        "未找到该帮助分类，或该插件未启用。"
        f"\n可用分类：{available}"
        "\n用法：@bot /help 分类名"
        "\n@bot /help 查看帮助首页；@bot /help 文本 查看全部文字帮助。"
    )


def _node(
    bot_qq: str,
    content: list[dict[str, Any]],
    *,
    nickname: str = "机器人帮助",
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "type": "node",
        "data": {"user_id": bot_qq, "nickname": nickname, "content": content, **metadata},
    }


def build_help_card(
    bot_qq: str | int | None,
    enabled_plugin_ids: tuple[str, ...] | None = None,
    plugin_settings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one send_group_forward_msg payload, without a destination or cached resource IDs."""
    sender_id = str(bot_qq).strip() if bot_qq is not None else ""
    if not sender_id.isascii() or not sender_id.isdecimal() or int(sender_id) <= 0:
        raise ValueError("Missing or invalid bot QQ for help card")

    sections = get_help_sections(enabled_plugin_ids, plugin_settings)
    available = "、".join(section.title for section in sections) or "暂无可用分类"
    intro = (
        "功能导航\n点击下方分类卡片，查看该分类的指令说明。"
        "\n使用 QQ 的返回按钮回到上一级。"
        f"\n分类：{available}"
        "\n\n卡片仅供浏览，不会自动执行指令；执行时请回到聊天中真正 @bot 后输入指令。"
        "\n全部文字帮助：@bot /help 文本"
        "\n指定分类文字帮助：@bot /help 分类名"
    )
    nodes = [_node(sender_id, [text_segment(intro)], nickname="导航说明")]
    for section in sections:
        # Nested nodes are rendered as clickable category cards by NapCat/QQ.
        nodes.append(_node(
            sender_id,
            [_node(sender_id, [text_segment(line)]) for line in section.lines],
            nickname=section.title,
            source=f"{section.title}｜帮助分类",
            summary=f"{len(section.lines)} 条指令说明",
            prompt=f"[{section.title}帮助]",
            news=[{"text": line} for line in section.lines[:3]],
        ))

    previews = [{"text": section.title} for section in sections[:3]]
    if len(sections) > 3:
        previews.append({"text": f"共 {len(sections)} 个分类，点击查看全部"})
    return {
        "message": nodes,
        "source": "功能导航",
        "summary": f"{len(sections)} 个功能分类",
        "prompt": "[功能导航]",
        "news": previews or [{"text": "暂无可用指令分类"}],
    }
