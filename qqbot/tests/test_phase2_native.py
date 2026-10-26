"""Phase 2 checks for the native NoneBot boundary and retired summary feature."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.plugin_catalog import PLUGIN_ORDER, without_retired_plugins
from infrastructure.config import load_runtime_config
from webui.config_service import ConfigService


ROOT = Path(__file__).resolve().parents[1]


def test_summary_is_not_registered_or_in_effective_payload(tmp_path):
    payload = {
        "enabled_plugins": ["summary", "basic"],
        "plugin_settings": {"summary": {"deepseek_api_key": "secret"}, "basic": {}},
        "extension": {"keep": "yes"},
    }
    effective = without_retired_plugins(payload)
    assert "summary" not in PLUGIN_ORDER
    assert effective["enabled_plugins"] == ["basic"]
    assert "summary" not in effective["plugin_settings"]
    assert effective["extension"] == {"keep": "yes"}


def test_runtime_config_loads_absolute_paths_and_strict_driver(tmp_path):
    config = tmp_path / "plugin_config.json"
    config.write_text(json.dumps({"enabled_plugins": ["basic", "summary"], "plugin_settings": {}}), encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    project = tmp_path / "pyproject.toml"
    project.write_text("[tool.nonebot]\nplugins=[]\n", encoding="utf-8")
    values = {
        "HOST": "127.0.0.1",
        "PORT": "18080",
        "DRIVER": "~fastapi+~httpx+~websockets",
        "LOG_LEVEL": "INFO",
        "BOT_PLUGIN_CONFIG": str(config),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "ASSETS_ROOT": str(assets),
        "PROJECT_FILE": str(project),
        "WEBSOCKET_MODE": "server",
        "SUPERUSERS": "[\"123\"]",
    }
    loaded, payload, version = load_runtime_config(values)
    assert loaded.port == 18080
    assert payload["enabled_plugins"] == ["basic"]
    assert len(version) == 64


def test_strict_mention_uses_original_onebot_message_segments():
    source = (ROOT / "plugins" / "core" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "original_message" in source
    assert "没有该指令喵" in source
    assert "to_me" not in source.split("def command_after_real_mention", 1)[1].split("async def real_mention", 1)[0]
    # The implementation intentionally checks the original at segment, not text aliases.
    assert any(isinstance(node, ast.FunctionDef) and node.name == "command_after_real_mention" for node in tree.body)


def test_strict_mention_function_accepts_real_at_only():
    import nonebot

    nonebot.init(driver="~none", command_start={"/"})
    from plugins.core import command_after_real_mention

    real = SimpleNamespace(
        self_id=999,
        original_message=[
            SimpleNamespace(type="at", data={"qq": "999"}),
            SimpleNamespace(type="text", data={"text": " /hello"}),
        ],
    )
    fake_text = SimpleNamespace(
        self_id=999,
        original_message=[SimpleNamespace(type="text", data={"text": "@bot /hello"})],
    )
    assert command_after_real_mention(real) == "/hello"
    assert command_after_real_mention(fake_text) is None


def test_native_entrypoint_has_no_legacy_dispatcher():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert {"nonebot", "Adapter", "load_application_plugins"} <= names
    assert "NapCatBot" not in source
    assert "NativeWebSocket" not in source
    assert "asyncio" not in source


def test_management_plugin_declares_protected_status_and_lifecycle_hooks():
    source = (ROOT / "plugins" / "management" / "__init__.py").read_text(encoding="utf-8")
    assert "/internal/management/status" in source
    assert "on_startup" in source and "on_shutdown" in source
    assert "on_bot_connect" in source and "on_bot_disconnect" in source
    assert "Bearer" in source and "compare_digest" in source


def test_webui_reads_management_status_without_returning_the_token():
    source = (ROOT / "webui" / "management_client.py").read_text(encoding="utf-8")
    assert "management.token" in source
    assert "Authorization" in source
    assert "return token" not in source


def test_webui_effective_config_drops_retired_summary_fields(tmp_path):
    path = tmp_path / "plugin_config.json"
    path.write_text(json.dumps({
        "enabled_plugins": ["summary", "basic"],
        "plugin_settings": {"summary": {"deepseek_api_key": "private"}},
    }), encoding="utf-8")
    value = ConfigService(tmp_path).read()
    assert value["enabled_plugins"] == ["basic"]
    assert all(item["plugin_id"] != "summary" for item in value["definitions"])
    assert "private" not in json.dumps(value, ensure_ascii=False)


def test_management_distinguishes_disabled_migrated_plugin(tmp_path, monkeypatch):
    import nonebot
    from infrastructure.runtime import RuntimeState

    project = tmp_path / "pyproject.toml"
    project.write_text("[tool.nonebot]\nplugins=[]\n", encoding="utf-8")
    package = tmp_path / "plugins" / "courtship"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    state = RuntimeState()
    state.configure(SimpleNamespace(project_file=project), {"enabled_plugins": []}, "test")
    monkeypatch.setattr(nonebot, "get_loaded_plugins", lambda: set())
    monkeypatch.setattr(nonebot, "get_bots", lambda: {})
    plugins = {item["id"]: item for item in state.status()["business_plugins"]}

    assert plugins["courtship"] == {"id": "courtship", "requested": False, "state": "disabled"}
    assert plugins["song"] == {"id": "song", "requested": False, "state": "pending_migration"}


def test_standard_third_party_plugin_loads_and_exposes_metadata():
    import nonebot

    plugin = nonebot.load_plugin("nonebot_plugin_apscheduler")
    assert plugin is not None
    assert plugin.module_name == "nonebot_plugin_apscheduler"
    assert plugin.metadata is not None
    assert plugin.metadata.name


def test_bootstrap_deduplicates_official_plugin_declarations(tmp_path, monkeypatch):
    import nonebot
    from infrastructure.bootstrap import load_application_plugins

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.nonebot]\nplugins=["nonebot_plugin_apscheduler", "nonebot_plugin_apscheduler"]\n',
        encoding="utf-8",
    )
    loaded = []
    monkeypatch.setattr(nonebot, "load_plugin", lambda module: loaded.append(module) or object())
    load_application_plugins(project, {"enabled_plugins": []})
    assert loaded == ["plugins.core", "plugins.management", "nonebot_plugin_apscheduler"]


@pytest.mark.parametrize(
    "declaration",
    (
        '[tool.nonebot]\nplugins=["plugins.summary"]\n',
        '[tool.nonebot]\nplugins="nonebot_plugin_apscheduler"\n',
        '[tool.nonebot]\n',
    ),
)
def test_bootstrap_rejects_invalid_plugin_declarations(tmp_path, declaration):
    from infrastructure.bootstrap import load_application_plugins

    project = tmp_path / "pyproject.toml"
    project.write_text(declaration, encoding="utf-8")
    with pytest.raises(ValueError, match="插件声明无效"):
        load_application_plugins(project, {"enabled_plugins": []})


def test_bootstrap_fails_safely_when_declared_plugin_cannot_load(tmp_path, monkeypatch):
    import nonebot
    from infrastructure.bootstrap import load_application_plugins

    project = tmp_path / "pyproject.toml"
    project.write_text('[tool.nonebot]\nplugins=["missing_standard_plugin"]\n', encoding="utf-8")
    monkeypatch.setattr(
        nonebot,
        "load_plugin",
        lambda module: None if module == "missing_standard_plugin" else object(),
    )
    with pytest.raises(RuntimeError, match="missing_standard_plugin"):
        load_application_plugins(project, {"enabled_plugins": []})

