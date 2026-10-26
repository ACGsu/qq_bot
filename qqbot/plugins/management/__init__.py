"""Internal routes and lifecycle run in NoneBot's FastAPI driver."""
import hmac
import os
import secrets
from fastapi import Header, HTTPException
from nonebot import get_driver
from nonebot.adapters import Bot
from nonebot.plugin import PluginMetadata
from infrastructure.runtime import runtime

__plugin_meta__ = PluginMetadata(name="运行管理", description="实际插件、连接、配置版本和生命周期", usage="GET /internal/management/status；独立 Bearer 令牌")
driver = get_driver()
app = driver.server_app
access_token = ""

@driver.on_startup
async def startup():
    global access_token
    for name in ("db", "downloads", "cache", "webui"):
        (runtime.config.storage_root / name).mkdir(parents=True, exist_ok=True)
    access_token = runtime.config.management_access_token.get_secret_value() or secrets.token_urlsafe(48)
    path = runtime.config.management_token_path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(access_token)
    runtime.started()

@driver.on_shutdown
async def shutdown():
    global access_token
    runtime.phase = "stopped"
    access_token = ""
    runtime.config.management_token_path.unlink(missing_ok=True)

@driver.on_bot_connect
async def connected(bot: Bot):
    runtime.connect_count += 1

@driver.on_bot_disconnect
async def disconnected(bot: Bot):
    runtime.disconnect_count += 1

@app.get("/internal/management/status", include_in_schema=False)
async def status(authorization: str | None = Header(default=None)):
    expected = f"Bearer {access_token}"
    if not access_token or not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="管理接口鉴权失败")
    return runtime.status()
