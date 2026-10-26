from dataclasses import dataclass
from pathlib import Path
from dotenv import dotenv_values
import os

APP_ROOT = Path(__file__).resolve().parent.parent
ROOT = APP_ROOT.parent

@dataclass(frozen=True)
class Settings:
    password: str
    root: Path = ROOT
    port: int = 8765
    session_seconds: int = 3600
    host: str = "127.0.0.1"
    config_path: Path | None = None
    runtime_dir: Path | None = None
    public_port: int | None = None
    container_mode: bool = False

    def __post_init__(self):
        if not isinstance(self.password, str) or not 16 <= len(self.password) <= 512 or not self.password.strip():
            raise ValueError("WEBUI_PASSWORD 必须设置为 16～512 字符的独立密码，不能全为空白")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("WEBUI_PORT 必须在 1024～65535 范围")
        if self.public_port is not None and (type(self.public_port) is not int or not 1024 <= self.public_port <= 65535):
            raise ValueError("WEBUI_PUBLIC_PORT 必须在 1024～65535 范围")
        if self.host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            raise ValueError("WEBUI_HOST 必须为 127.0.0.1、localhost 或 0.0.0.0")

    @classmethod
    def load(cls):
        root = Path(os.environ.get("WEBUI_PROJECT_ROOT", str(ROOT)))
        default_env_file = root / "qqbot" / "config" / "webui.env"
        if not default_env_file.is_file():
            default_env_file = root / ".webui.env"
        env_file = Path(os.environ.get("WEBUI_ENV_FILE", str(default_env_file)))
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        try:
            port = int(values.get("WEBUI_PORT", "8765"))
            public_port = int(values.get("WEBUI_PUBLIC_PORT", str(port)))
        except (TypeError, ValueError):
            raise ValueError("WebUI 端口必须为 1024～65535 的整数") from None
        return cls(password=values.get("WEBUI_PASSWORD", ""), root=root, port=port,
                   host=values.get("WEBUI_HOST", "127.0.0.1"), public_port=public_port,
                   config_path=Path(values.get("WEBUI_CONFIG_PATH") or APP_ROOT / "config" / "plugin_config.json"),
                   runtime_dir=Path(values.get("WEBUI_RUNTIME_DIR") or APP_ROOT / "storage" / "webui"),
                   container_mode=values.get("WEBUI_CONTAINER") == "1")

    @property
    def runtime_path(self):
        return self.runtime_dir if self.runtime_dir is not None else self.root

    @property
    def origins(self):
        port = self.public_port if self.public_port is not None else self.port
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
