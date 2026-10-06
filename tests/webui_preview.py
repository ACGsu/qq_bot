"""Isolated manual browser fixture: never touches the real config or Docker.
Run from the project root: python tests/webui_preview.py
"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from plugin_control import PLUGIN_ORDER
from webui.app import create_app
from webui.settings import Settings
from webui_support import FakeDocker, PASSWORD

if __name__ == "__main__":
    with TemporaryDirectory(prefix="qqbot-webui-preview-") as directory:
        root = Path(directory)
        (root / "plugin_config.json").write_text(json.dumps({"enabled_plugins":list(PLUGIN_ORDER),
            "plugin_settings":{"auto_emoji":{"emoji_ids":"127852,12951,99999"},
                               "summary":{"deepseek_api_key":"fake-test-key"}}}), encoding="utf-8")
        uvicorn.run(create_app(Settings(password=PASSWORD,root=root,port=18765),runner=FakeDocker()),
                    host="127.0.0.1",port=18765,proxy_headers=False,access_log=False)
