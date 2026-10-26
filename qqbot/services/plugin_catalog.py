"""Pure plugin metadata and configuration normalization rules."""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PluginDefinition:
    plugin_id: str
    name: str
    description: str
    commands: tuple[str, ...]


@dataclass(frozen=True)
class PluginConfig:
    enabled_plugin_ids: tuple[str, ...]
    plugin_settings: dict[str, dict[str, str]]


PLUGIN_DEFINITIONS: tuple[PluginDefinition, ...] = (
    PluginDefinition("auto_emoji", "自动贴表情", "指定 QQ 号在群聊发言时，自动给该消息贴 🍬。", ("被动监听",)),
    PluginDefinition("rps", "猜拳", "发起群成员猜拳，输家按配置时长禁言。", ("/猜拳 @成员",)),
    PluginDefinition("daily_wife", "今日群友", "每日随机抽取群友，可独立开关结芬及回应流程。",
                     ("/今日群友", "/今日老婆", "/结芬", "/愿意", "/不愿意")),
    PluginDefinition("courtship", "求偶", "向指定群成员发起求偶请求并等待对方回应。",
                     ("/求偶 @成员", "/接受求偶", "/拒绝求偶")),
    PluginDefinition("song", "歌曲下载", "搜索歌曲、选择候选并按指定音质下载源文件发送；关键词加 +塞壬唱片 可搜塞壬唱片。",
                     ("/song 歌曲名", "/song 序号", "/song 序号+音质", "/song 歌曲名+塞壬唱片")),
    PluginDefinition("novel", "小说链接", "查询轻小说目录，并返回章节或卷阅读链接。",
                     ("/novel 小说名", "/第1章", "/第1卷")),
    PluginDefinition("timetable", "课表", "导入或更新本人的 ICS 课表，查询本群当前课程和本人今日课程；可关闭群聊隔离以跨群共享，不支持 Excel。",
                     ("/课表", "/导入课表", "/已导入", "/更新课表", "/删除课表", "/课ing", "/今日课程", "/取消导入")),
    PluginDefinition("basic", "基础指令", "提供可点击的分层帮助、文字帮助和 hello 测试回复。", ("/help", "/hello")),
)

PLUGIN_ORDER: tuple[str, ...] = tuple(plugin.plugin_id for plugin in PLUGIN_DEFINITIONS)
PLUGIN_BY_ID: dict[str, PluginDefinition] = {plugin.plugin_id: plugin for plugin in PLUGIN_DEFINITIONS}
NATIVE_MODULE_PLUGIN_IDS = frozenset({"song", "timetable"})


def default_enabled_plugin_ids() -> tuple[str, ...]:
    return PLUGIN_ORDER


def normalize_enabled_plugin_ids(enabled_plugin_ids: Iterable[str] | None) -> tuple[str, ...]:
    if enabled_plugin_ids is None:
        return default_enabled_plugin_ids()
    enabled = {str(plugin_id) for plugin_id in enabled_plugin_ids}
    return tuple(plugin_id for plugin_id in PLUGIN_ORDER if plugin_id in enabled)


def parse_bool_setting(value: object, default: bool = False) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return default


def normalize_plugin_settings(settings: object) -> dict[str, dict[str, str]]:
    if not isinstance(settings, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for plugin_id, plugin_settings in settings.items():
        plugin_id = str(plugin_id)
        if plugin_id not in PLUGIN_BY_ID or not isinstance(plugin_settings, dict):
            continue
        normalized[plugin_id] = {str(key): str(value) for key, value in plugin_settings.items()}
    return normalized


def validate_ban_seconds(value: object) -> int:
    """Strict management boundary; runtime callers may explicitly fall back."""
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value) or len(value) > 5:
        raise ValueError("猜拳禁言时长必须为 1～86400 的整数秒")
    seconds = int(value)
    if not 1 <= seconds <= 86400:
        raise ValueError("猜拳禁言时长必须为 1～86400 的整数秒")
    return seconds


def without_retired_plugins(payload: dict) -> dict:
    """Remove retired feature fields while preserving unrelated extensions."""
    import copy
    result = copy.deepcopy(payload)
    result["enabled_plugins"] = [p for p in result.get("enabled_plugins", []) if p != "summary"]
    result.setdefault("plugin_settings", {}).pop("summary", None)
    if isinstance(result.get("plugins"), list):
        result["plugins"] = [p for p in result["plugins"] if not isinstance(p, dict) or p.get("plugin_id") != "summary"]
    return result
