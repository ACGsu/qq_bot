"""Strict and tolerant JSON persistence for plugin configuration."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.plugin_catalog import (
    PLUGIN_DEFINITIONS,
    PLUGIN_ORDER,
    PluginConfig,
    default_enabled_plugin_ids,
    normalize_enabled_plugin_ids,
    normalize_plugin_settings,
    validate_ban_seconds,
)


def get_plugin_config_path() -> Path:
    configured_path = os.getenv("BOT_PLUGIN_CONFIG")
    return Path(configured_path) if configured_path else Path(__file__).resolve().parents[1] / "config" / "plugin_config.json"


def load_plugin_config(config_path: Path | None = None) -> PluginConfig:
    path = config_path or get_plugin_config_path()
    if not path.exists():
        return PluginConfig(default_enabled_plugin_ids(), {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PluginConfig(default_enabled_plugin_ids(), {})
    enabled_ids = payload.get("enabled_plugins") if isinstance(payload, dict) else None
    enabled = normalize_enabled_plugin_ids(enabled_ids) if isinstance(enabled_ids, list) else default_enabled_plugin_ids()
    settings = normalize_plugin_settings(payload.get("plugin_settings") if isinstance(payload, dict) else None)
    return PluginConfig(enabled, settings)


def load_enabled_plugin_ids(config_path: Path | None = None) -> tuple[str, ...]:
    return load_plugin_config(config_path).enabled_plugin_ids


def load_plugin_settings(plugin_id: str, config_path: Path | None = None) -> dict[str, str]:
    return dict(load_plugin_config(config_path).plugin_settings.get(plugin_id, {}))


def save_plugin_config(enabled_plugin_ids: Iterable[str], plugin_settings: dict[str, dict[str, str]] | None = None,
                       config_path: Path | None = None) -> Path:
    path = config_path or get_plugin_config_path()
    payload = {
        "enabled_plugins": list(normalize_enabled_plugin_ids(enabled_plugin_ids)),
        "plugin_settings": normalize_plugin_settings(plugin_settings),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "plugins": [asdict(plugin) for plugin in PLUGIN_DEFINITIONS],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return path


def save_enabled_plugin_ids(enabled_plugin_ids: Iterable[str], config_path: Path | None = None) -> Path:
    return save_plugin_config(enabled_plugin_ids, load_plugin_config(config_path).plugin_settings, config_path)


def load_plugin_payload_strict(path: Path) -> tuple[dict, str]:
    """Read without tolerant fallback. Never expose file contents in errors."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"enabled_plugins": list(PLUGIN_ORDER), "plugin_settings": {}}, "missing"
    except OSError:
        raise ValueError("配置读取失败，请检查文件权限") from None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        enabled = payload.get("enabled_plugins")
        settings = payload.get("plugin_settings", {})
        if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
            raise ValueError()
        if not isinstance(settings, dict) or any(not isinstance(value, dict) for value in settings.values()):
            raise ValueError()
        for plugin_id, values in settings.items():
            for key, value in values.items():
                legacy_boolean = (plugin_id, key) in {
                    ("daily_wife", "marriage_enabled"), ("timetable", "group_isolation_enabled")}
                if not isinstance(value, str) and not (legacy_boolean and isinstance(value, bool)):
                    raise ValueError()
        if "ban_seconds" in settings.get("rps", {}):
            validate_ban_seconds(settings["rps"]["ban_seconds"])
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("配置损坏或字段格式无效，请先修复；未覆盖原文件") from None
    return payload, hashlib.sha256(raw).hexdigest()


def atomic_write_plugin_payload(path: Path, payload: dict) -> None:
    """Atomically replace one file without creating a backup copy."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".webui-", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
