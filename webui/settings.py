from dataclasses import dataclass
from pathlib import Path
from dotenv import dotenv_values
import os

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Settings:
    password: str
    root: Path = ROOT
    port: int = 8765
    session_seconds: int = 3600

    def __post_init__(self):
        if not isinstance(self.password, str) or not 16 <= len(self.password) <= 512 or not self.password.strip():
            raise ValueError("WEBUI_PASSWORD 必须设置为 16～512 字符的独立密码，不能全为空白")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("WEBUI_PORT 必须在 1024～65535 范围")

    @classmethod
    def load(cls):
        values = {**dotenv_values(ROOT / ".webui.env", interpolate=False), **os.environ}
        try:
            port = int(values.get("WEBUI_PORT", "8765"))
        except (TypeError, ValueError):
            raise ValueError("WEBUI_PORT 必须为 1024～65535 的整数") from None
        return cls(password=values.get("WEBUI_PASSWORD", ""), port=port)

    @property
    def origins(self):
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
