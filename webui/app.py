import hmac
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import Timeout
from .config_service import ConfigService, Conflict
from .docker_service import DockerService, Busy, DockerFailure
from .emoji_codec import encode, preview
from .schemas import Login, ConfigEdit, EmojiInput, Action
from .security import COOKIE, Security
from .settings import Settings

STATIC = Path(__file__).parent / "static"

def create_app(settings=None, runner=None):
    settings = settings or Settings.load()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    config = ConfigService(settings.root)
    docker = DockerService(config, **({"runner": runner} if runner else {}))
    docker.known_secrets.add(settings.password)
    auth = Security(settings)
    app.state.config, app.state.docker, app.state.auth = config, docker, auth

    @app.middleware("http")
    async def protect(request: Request, call_next):
        host = request.headers.get("host", "")
        if f"http://{host}" not in settings.origins:
            return JSONResponse({"detail": "不允许的 Host"}, status_code=403)
        origin = request.headers.get("origin")
        if (origin and origin not in settings.origins) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
        if request.method not in {"GET", "HEAD"} and origin not in settings.origins:
            return JSONResponse({"detail": "缺少同源请求标识"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.url.path != "/api/login":
                session = auth.get(request.cookies.get(COOKIE))
                if not session:
                    return JSONResponse({"detail": "请登录，或会话已过期"}, status_code=401)
                request.state.session = session
                if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session["csrf"].encode()):
                    return JSONResponse({"detail": "CSRF 校验失败，请重新登录"}, status_code=403)
            if request.method not in {"GET", "HEAD"}:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 32768:
                        return JSONResponse({"detail": "请求过大"}, status_code=413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "请求格式无效，请检查输入字段（秘密值不会回显）"}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        code = 409 if isinstance(exc, (Conflict, Busy)) else 503 if isinstance(exc, DockerFailure) else 422
        return JSONResponse({"detail": docker.clean(str(exc))}, status_code=code)

    @app.exception_handler(Timeout)
    async def lock_error(request, exc):
        return JSONResponse({"detail": "配置正在被其他操作使用，请稍后重试"}, status_code=409)

    @app.exception_handler(OSError)
    async def io_error(request, exc):
        return JSONResponse({"detail": "本地文件访问失败，请检查权限；未确认保存或应用成功"}, status_code=500)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    def login(data: Login):
        result, reason = auth.login(data.password)
        if not result:
            return JSONResponse({"detail": "尝试过于频繁，请 60 秒后重试" if reason == "rate" else "密码错误"}, status_code=429 if reason == "rate" else 401)
        token, session = result
        response = JSONResponse({"csrf": session["csrf"]})
        response.set_cookie(COOKIE, token, max_age=settings.session_seconds, httponly=True, samesite="strict", secure=False)
        return response

    @app.get("/api/session")
    def session(request: Request):
        return {"csrf": request.state.session["csrf"]}

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/config")
    def read_config():
        return config.read()

    @app.post("/api/config/validate")
    def validate_config(data: ConfigEdit):
        config.save(data, validate_only=True)
        return {"ok": True}

    @app.put("/api/config")
    def save_config(data: ConfigEdit):
        with docker.exclusive():
            with docker.redaction_lock:
                docker.known_secrets.update(config.secrets())
            result = config.save(data)
            docker.last_application = "已保存待应用"
            return result

    @app.post("/api/config/apply")
    def apply_config(data: ConfigEdit):
        return docker.submit("apply", data)

    @app.post("/api/emoji/preview")
    def emoji_preview(data: EmojiInput):
        return {"emoji": preview(encode(data.text)), "notice": "按 Unicode 单码点转换回应 ID；解析成功不代表当前 QQ／NapCat 一定支持，实际接受情况需授权群验证"}

    @app.get("/api/status")
    def status():
        return docker.status()

    @app.get("/api/logs")
    def logs():
        return {"text": docker.logs()}

    @app.get("/api/tasks")
    def tasks():
        return {"tasks": docker.list_tasks()}

    @app.post("/api/tasks")
    def task(data: Action):
        return docker.submit(data.action)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
