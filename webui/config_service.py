import copy
import os
import re
from dataclasses import asdict
from urllib.parse import urlsplit
from filelock import FileLock
from dotenv import dotenv_values
from plugin_control import (PLUGIN_ORDER, PLUGIN_DEFINITIONS, load_plugin_payload_strict,
                            atomic_write_plugin_payload, validate_ban_seconds)
from .emoji_codec import encode, tokens, preview

FIELDS = {"auto_emoji": {"target_qq"}, "rps": {"ban_seconds"},
          "daily_wife": {"marriage_enabled"}, "timetable": {"group_isolation_enabled"},
          "summary": {"deepseek_api_key", "deepseek_api_base_url", "deepseek_model"}}
KEY = "deepseek_api_key"

class Conflict(ValueError):
    pass

class ConfigService:
    def __init__(self, root):
        self.root = root
        self.path = root / "plugin_config.json"
        self.lock = FileLock(str(root / ".webui.lock"), timeout=3)
        self.applied_version = None

    def environment(self):
        # Read only, do not load into process environment or return arbitrary values.
        return {**dotenv_values(self.root / ".env", interpolate=False), **os.environ}

    def effective_emoji(self, payload):
        settings = payload.get("plugin_settings", {}).get("auto_emoji", {})
        env = self.environment()
        return tokens(settings.get("emoji_ids") or settings.get("emoji_id") or
                      env.get("AUTO_EMOJI_IDS") or env.get("AUTO_CANDY_EMOJI_ID") or
                      env.get("AUTO_EMOJI_ID") or "127852,12951")

    def read(self):
        payload, version = load_plugin_payload_strict(self.path)
        original = payload.get("plugin_settings", {})
        settings = {p: {k: str(v).lower() if isinstance(v, bool) else v for k, v in original.get(p, {}).items() if k in fields and k != KEY}
                    for p, fields in FIELDS.items()}
        env = self.environment()
        summary = original.get("summary", {})
        return {"version": version, "enabled_plugins": payload["enabled_plugins"],
                "plugin_settings": settings, "definitions": [asdict(p) for p in PLUGIN_DEFINITIONS],
                "api_key_set": bool(summary.get(KEY) or env.get("DEEPSEEK_API_KEY")),
                "api_key_source": "JSON" if summary.get(KEY) else "环境或未设置",
                "emoji": preview(self.effective_emoji(payload)),
                "emoji_source": "JSON" if original.get("auto_emoji", {}).get("emoji_ids") or original.get("auto_emoji", {}).get("emoji_id") else "继承环境配置／默认值",
                "application": "已验证应用" if version == self.applied_version else "应用状态未知／待应用"}

    def prepare(self, edit):
        payload, version = load_plugin_payload_strict(self.path)
        if edit.version != version:
            raise Conflict("配置已被其他页面或程序修改，请重新加载后再编辑")
        if any(p not in PLUGIN_ORDER for p in edit.enabled_plugins):
            # Existing unknown plugin IDs must survive a UI save but cannot be newly injected.
            if set(edit.enabled_plugins) - set(PLUGIN_ORDER) - set(payload["enabled_plugins"]):
                raise ValueError("未知插件")
        payload = copy.deepcopy(payload)
        payload["enabled_plugins"] = list(dict.fromkeys(edit.enabled_plugins))
        settings = payload.setdefault("plugin_settings", {})
        for plugin, values in edit.plugin_settings.items():
            if plugin not in FIELDS or set(values) - FIELDS[plugin]:
                raise ValueError("不支持的设置字段")
            target = settings.setdefault(plugin, {})
            for key, value in values.items():
                if len(value) > 4096:
                    raise ValueError("设置内容过长")
                if key == KEY and not value:
                    continue
                if key == "ban_seconds":
                    validate_ban_seconds(value)
                if key in {"marriage_enabled", "group_isolation_enabled"} and value not in {"true", "false"}:
                    raise ValueError("子功能开关必须为 true 或 false")
                if key == "target_qq" and value and not re.fullmatch(r"[0-9]{5,20}", value):
                    raise ValueError("目标 QQ 必须为 5～20 位数字")
                if key == "deepseek_api_base_url" and value:
                    url = urlsplit(value)
                    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                        raise ValueError("API 地址须为不含凭据、查询参数的 HTTP(S) URL")
                target[key] = value
        if edit.clear_api_key:
            settings.setdefault("summary", {}).pop(KEY, None)
        if edit.emoji_text is not None or edit.retained_emoji_ids is not None:
            old = self.effective_emoji(payload)
            retained = edit.retained_emoji_ids or []
            if any(x not in old for x in retained):
                raise ValueError("只能保留已有表情；新增请直接输入表情")
            new = encode(edit.emoji_text) if edit.emoji_text else []
            ids = list(dict.fromkeys(retained + new))
            if not ids:
                raise ValueError("至少保留一个表情，或关闭自动贴表情插件")
            settings.setdefault("auto_emoji", {})["emoji_ids"] = ",".join(ids)
        return payload

    def save(self, edit, validate_only=False):
        with self.lock:
            payload = self.prepare(edit)
            if not validate_only:
                atomic_write_plugin_payload(self.path, payload)
        return self.read()

    def secrets(self):
        env = self.environment()
        values = [str(v) for k, v in env.items() if v and any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE"))]
        try:
            payload, _ = load_plugin_payload_strict(self.path)
            values.extend(str(v) for settings in payload.get("plugin_settings", {}).values() for k, v in settings.items()
                          if v and any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE")))
        except ValueError:
            pass
        return values
