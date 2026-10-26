"""Validate deployment settings before NoneBot creates its driver."""
import json
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from infrastructure.plugin_config import load_plugin_payload_strict
from services.plugin_catalog import parse_bool_setting, without_retired_plugins

APP_ROOT = Path(__file__).resolve().parents[1]


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    driver: str = "~fastapi+~httpx+~websockets"
    log_level: str = "INFO"
    onebot_access_token: SecretStr = SecretStr("")
    management_access_token: SecretStr = SecretStr("")
    core_selftest_enabled: bool = False
    superusers: set[str] = Field(default_factory=set)
    bot_plugin_config: Path = APP_ROOT / "config" / "plugin_config.json"
    storage_root: Path = APP_ROOT / "storage"
    assets_root: Path = APP_ROOT / "assets"
    project_file: Path = APP_ROOT / "pyproject.toml"
    song_music_source: str = "netease"
    song_bitrate: str = "320"
    song_session_minutes: int = Field(default=10, ge=1, le=1440)
    song_max_download_mb: int = Field(default=100, ge=1, le=1024)
    song_base64_upload_limit_mb: int = Field(default=45, ge=1, le=100)
    song_stream_chunk_kb: int = Field(default=256, ge=64, le=4096)
    song_cache_ttl_hours: int = Field(default=24, ge=1, le=168)
    song_download_dir: Path = APP_ROOT / "storage" / "downloads"
    song_cover_size: str = "300"
    gd_music_api_url: str = "https://music-api.gdstudio.xyz/api.php"
    gd_music_site_url: str = "https://music.gdstudio.xyz"
    siren_api_base_url: str = "https://monster-siren.hypergryph.com"
    siren_catalog_ttl_minutes: int = Field(default=10, ge=1, le=1440)
    timetable_db_path: Path = APP_ROOT / "storage" / "db" / "timetable.sqlite3"

    @property
    def management_token_path(self) -> Path:
        return self.storage_root / "webui" / "management.token"

    def nonebot_options(self, payload: dict | None = None, environ=None) -> dict:
        """Build framework and migrated plugin config with explicit JSON priority."""
        env = os.environ if environ is None else environ
        settings = (payload or {}).get("plugin_settings", {})
        rps = settings.get("rps", {}) if isinstance(settings, dict) else {}
        auto_emoji = settings.get("auto_emoji", {}) if isinstance(settings, dict) else {}
        daily_wife = settings.get("daily_wife", {}) if isinstance(settings, dict) else {}
        timetable = settings.get("timetable", {}) if isinstance(settings, dict) else {}

        if "target_qq" in auto_emoji:
            target_qq = auto_emoji["target_qq"]
        else:
            target_qq = env.get("AUTO_CANDY_QQ") or env.get("AUTO_EMOJI_QQ") or ""
        if "emoji_ids" in auto_emoji:
            emoji_ids = auto_emoji["emoji_ids"]
        elif "emoji_id" in auto_emoji:
            emoji_ids = auto_emoji["emoji_id"]
        else:
            emoji_ids = (env.get("AUTO_EMOJI_IDS") or env.get("AUTO_CANDY_EMOJI_ID")
                         or env.get("AUTO_EMOJI_ID") or "127852,12951")

        return {"host": self.host, "port": self.port, "driver": self.driver,
                "log_level": self.log_level, "onebot_access_token": self.onebot_access_token.get_secret_value() or None,
                "core_selftest_enabled": self.core_selftest_enabled, "superusers": self.superusers,
                "rps_ban_seconds": int(rps.get("ban_seconds", "60")),
                "auto_emoji_target_qq": str(target_qq).strip(),
                "auto_emoji_ids": str(emoji_ids),
                "daily_wife_marriage_enabled": parse_bool_setting(
                    daily_wife.get("marriage_enabled"), default=True
                ),
                "song_music_source": self.song_music_source,
                "song_bitrate": self.song_bitrate,
                "song_session_minutes": self.song_session_minutes,
                "song_max_download_mb": self.song_max_download_mb,
                "song_base64_upload_limit_mb": self.song_base64_upload_limit_mb,
                "song_stream_chunk_kb": self.song_stream_chunk_kb,
                "song_cache_ttl_hours": self.song_cache_ttl_hours,
                "song_download_dir": self.song_download_dir,
                "song_cover_size": self.song_cover_size,
                "gd_music_api_url": self.gd_music_api_url,
                "gd_music_site_url": self.gd_music_site_url,
                "siren_api_base_url": self.siren_api_base_url,
                "siren_catalog_ttl_minutes": self.siren_catalog_ttl_minutes,
                "timetable_db_path": self.timetable_db_path,
                "timetable_group_isolation_enabled": parse_bool_setting(
                    timetable.get("group_isolation_enabled"), default=True
                ),
                "command_start": {"/"}}


def load_runtime_config(environ=None):
    env = os.environ if environ is None else environ
    values = {key: env[key.upper()] for key in RuntimeConfig.model_fields if key.upper() in env}
    for new, old in (("host", "LISTEN_HOST"), ("port", "LISTEN_PORT"), ("onebot_access_token", "NAPCAT_ACCESS_TOKEN")):
        if new not in values and old in env:
            values[new] = env[old]
    try:
        if "superusers" in values:
            values["superusers"] = json.loads(values["superusers"])
        config = RuntimeConfig.model_validate(values)
        if not config.host.strip() or config.log_level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError()
        if not all(part in config.driver.split("+") for part in ("~fastapi", "~httpx", "~websockets")):
            raise ValueError()
        if env.get("WEBSOCKET_MODE", "server").lower() != "server":
            raise ValueError()
        token = config.management_access_token.get_secret_value()
        if token and (len(token) < 32 or token == config.onebot_access_token.get_secret_value()):
            raise ValueError()
        if (not config.bot_plugin_config.is_absolute() or not config.storage_root.is_absolute()
                or not config.song_download_dir.is_absolute() or not config.timetable_db_path.is_absolute()):
            raise ValueError()
        if config.song_bitrate not in {"192", "320", "740", "999"} or not config.song_music_source.strip():
            raise ValueError()
        if not config.bot_plugin_config.is_file() or not config.assets_root.is_dir() or not config.project_file.is_file():
            raise ValueError()
    except (ValidationError, ValueError, TypeError):
        raise ValueError("部署配置无效：检查 HOST、PORT、DRIVER、绝对路径、SUPERUSERS 和独立管理令牌；默认使用反向 WebSocket") from None
    payload, version = load_plugin_payload_strict(config.bot_plugin_config)
    return config, without_retired_plugins(payload), version
