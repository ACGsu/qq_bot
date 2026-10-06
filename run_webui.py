"""Run a single local management process; deliberately no remote bind/reload."""
import sys
import uvicorn
from filelock import FileLock, Timeout
from webui.app import create_app
from webui.settings import Settings

def main():
    try:
        settings = Settings.load()
        with FileLock(str(settings.root / ".webui-server.lock"), timeout=0):
            uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port,
                        proxy_headers=False, access_log=False, log_level="warning")
    except (ValueError, Timeout):
        print("无法启动：请在 .webui.env 中设置16～512 字符的非空白 WEBUI_PASSWORD、有效端口，并确认没有另一个 WebUI 实例。", file=sys.stderr)
        return 1
    except OSError:
        print("无法启动：请检查 WebUI 配置文件及锁文件的访问权限。", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
