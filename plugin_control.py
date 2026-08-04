import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    PluginDefinition(
        "auto_emoji",
        "自动贴表情",
        "指定 QQ 号在群聊发言时，自动给该消息贴 🍬。",
        ("被动监听",),
    ),
    PluginDefinition(
        "rps",
        "猜拳",
        "发起群成员猜拳，输家禁言 5 分钟。",
        ("/猜拳 @成员",),
    ),
    PluginDefinition(
        "daily_wife",
        "今日群友",
        "每日随机抽取群友，并支持结芬回应流程。",
        ("/今日群友", "/结芬", "/愿意", "/不愿意"),
    ),
    PluginDefinition(
        "courtship",
        "求偶",
        "向指定群成员发起求偶请求并等待对方回应。",
        ("/求偶 @成员", "/接受求偶", "/拒绝求偶"),
    ),
    PluginDefinition(
        "song",
        "歌曲下载",
        "搜索歌曲、选择候选并下载转换为 MP3 发送。",
        ("/song 歌曲名", "/song 序号"),
    ),
    PluginDefinition(
        "novel",
        "小说链接",
        "查询轻小说目录，并返回章节或卷阅读链接。",
        ("/novel 小说名", "/第1章", "/第1卷"),
    ),
    PluginDefinition(
        "summary",
        "群聊总结",
        "调用 DeepSeek API 总结当前群聊最近 n 条消息，最多 500 条。",
        ("/总结 条数n",),
    ),
    PluginDefinition(
        "basic",
        "基础指令",
        "提供帮助列表和 hello 测试回复。",
        ("/help", "/hello"),
    ),
)

PLUGIN_ORDER: tuple[str, ...] = tuple(plugin.plugin_id for plugin in PLUGIN_DEFINITIONS)
PLUGIN_BY_ID: dict[str, PluginDefinition] = {plugin.plugin_id: plugin for plugin in PLUGIN_DEFINITIONS}


def get_plugin_config_path() -> Path:
    configured_path = os.getenv("BOT_PLUGIN_CONFIG")
    if configured_path:
        return Path(configured_path)
    return Path(__file__).resolve().parent / "plugin_config.json"


def default_enabled_plugin_ids() -> tuple[str, ...]:
    return PLUGIN_ORDER


def normalize_enabled_plugin_ids(enabled_plugin_ids: Iterable[str] | None) -> tuple[str, ...]:
    if enabled_plugin_ids is None:
        return default_enabled_plugin_ids()

    enabled = {str(plugin_id) for plugin_id in enabled_plugin_ids}
    return tuple(plugin_id for plugin_id in PLUGIN_ORDER if plugin_id in enabled)


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


def load_plugin_config(config_path: Path | None = None) -> PluginConfig:
    path = config_path or get_plugin_config_path()
    if not path.exists():
        return PluginConfig(default_enabled_plugin_ids(), {})

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PluginConfig(default_enabled_plugin_ids(), {})

    enabled_plugin_ids = payload.get("enabled_plugins") if isinstance(payload, dict) else None
    if not isinstance(enabled_plugin_ids, list):
        enabled = default_enabled_plugin_ids()
    else:
        enabled = normalize_enabled_plugin_ids(enabled_plugin_ids)

    settings = normalize_plugin_settings(payload.get("plugin_settings") if isinstance(payload, dict) else None)
    return PluginConfig(enabled, settings)


def load_enabled_plugin_ids(config_path: Path | None = None) -> tuple[str, ...]:
    return load_plugin_config(config_path).enabled_plugin_ids


def load_plugin_settings(plugin_id: str, config_path: Path | None = None) -> dict[str, str]:
    return dict(load_plugin_config(config_path).plugin_settings.get(plugin_id, {}))


def save_plugin_config(
    enabled_plugin_ids: Iterable[str],
    plugin_settings: dict[str, dict[str, str]] | None = None,
    config_path: Path | None = None,
) -> Path:
    path = config_path or get_plugin_config_path()
    enabled = normalize_enabled_plugin_ids(enabled_plugin_ids)
    settings = normalize_plugin_settings(plugin_settings)
    payload = {
        "enabled_plugins": list(enabled),
        "plugin_settings": settings,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "plugins": [asdict(plugin) for plugin in PLUGIN_DEFINITIONS],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return path


def save_enabled_plugin_ids(enabled_plugin_ids: Iterable[str], config_path: Path | None = None) -> Path:
    existing_settings = load_plugin_config(config_path).plugin_settings
    return save_plugin_config(enabled_plugin_ids, existing_settings, config_path)
