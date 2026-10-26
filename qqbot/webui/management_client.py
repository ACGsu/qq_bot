"""Read-only client for the native NoneBot management endpoint."""
import os
from pathlib import Path

import httpx


class ManagementClient:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.url = os.getenv("BOT_MANAGEMENT_URL", "http://qq-bot:8080/internal/management/status")

    def status(self) -> dict:
        token_path = self.runtime_dir / "management.token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if not token or len(token) > 4096:
                raise ValueError
            response = httpx.get(self.url, headers={"Authorization": f"Bearer {token}"}, timeout=2.0)
            if response.status_code != 200:
                return {"available": False, "reason": "管理接口未就绪"}
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            return {"available": True, "state": payload}
        except (OSError, ValueError, httpx.HTTPError, UnicodeError):
            return {"available": False, "reason": "管理接口不可验证"}
