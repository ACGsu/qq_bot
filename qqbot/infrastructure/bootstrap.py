"""Choose modules; NoneBot owns registration, dependencies and dispatch."""
from pathlib import Path
import tomllib
import nonebot
from services.plugin_catalog import NATIVE_MODULE_PLUGIN_IDS, PLUGIN_ORDER
from .runtime import runtime


def load_application_plugins(project_file: Path, payload: dict):
    try:
        declaration = tomllib.loads(project_file.read_text(encoding="utf-8"))["tool"]["nonebot"]
        modules = declaration["plugins"]
        if not isinstance(modules, list) or any(not isinstance(m, str) or m == "plugins.summary" for m in modules):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("pyproject.toml 的 NoneBot 插件声明无效") from None
    for module in dict.fromkeys(["plugins.core", "plugins.management", *modules]):
        if nonebot.load_plugin(module) is None:
            raise RuntimeError(f"NoneBot 插件加载失败：{module}")
    for plugin_id in dict.fromkeys(payload["enabled_plugins"]):
        if plugin_id not in PLUGIN_ORDER:
            runtime.pending_plugins.append({"id": plugin_id, "reason": "unknown_configuration"})
            continue
        package = project_file.parent / "plugins" / plugin_id / "__init__.py"
        module_path = project_file.parent / "plugins" / f"{plugin_id}.py"
        native_module = plugin_id in NATIVE_MODULE_PLUGIN_IDS and module_path.is_file()
        if not package.is_file() and not native_module:
            runtime.pending_plugins.append({"id": plugin_id, "reason": "pending_migration"})
            continue
        if nonebot.load_plugin(f"plugins.{plugin_id}") is None:
            raise RuntimeError(f"NoneBot 业务插件加载失败：{plugin_id}")
