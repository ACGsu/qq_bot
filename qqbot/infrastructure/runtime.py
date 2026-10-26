"""Startup snapshot; live status comes from NoneBot rather than JSON intent."""
import hashlib
import json
from datetime import datetime, timezone
import nonebot
from services.plugin_catalog import NATIVE_MODULE_PLUGIN_IDS, PLUGIN_ORDER


class RuntimeState:
    def configure(self, config, payload, source_version):
        self.config = config
        self.payload = payload
        self.source_version = source_version
        self.effective_version = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                         separators=(",", ":")).encode()).hexdigest()
        self.phase = "initialized"
        self.started_at = None
        self.pending_plugins = []
        self.connect_count = 0
        self.disconnect_count = 0

    def status(self):
        loaded = []
        for plugin in sorted(nonebot.get_loaded_plugins(), key=lambda p: p.module_name):
            meta = plugin.metadata
            loaded.append({"id": plugin.name, "module": plugin.module_name,
                           "name": meta.name if meta else plugin.name,
                           "description": meta.description if meta else "", "usage": meta.usage if meta else ""})
        loaded_ids = {p["id"] for p in loaded}
        requested = set(self.payload.get("enabled_plugins", []))
        plugin_root = self.config.project_file.parent / "plugins"

        def business_state(plugin_id):
            if plugin_id in loaded_ids:
                return "loaded"
            migrated = (plugin_root / plugin_id / "__init__.py").is_file()
            if plugin_id in NATIVE_MODULE_PLUGIN_IDS:
                migrated = migrated or (plugin_root / f"{plugin_id}.py").is_file()
            if migrated and plugin_id not in requested:
                return "disabled"
            return "pending_migration"

        return {"phase": self.phase, "started_at": self.started_at,
                "config": {"loaded_version": self.source_version, "effective_version": self.effective_version},
                "plugins": loaded, "pending_plugins": self.pending_plugins,
                "business_plugins": [{"id": p, "requested": p in requested,
                                      "state": business_state(p)} for p in PLUGIN_ORDER],
                "bots": [{"self_id": bot.self_id, "adapter": bot.adapter.get_name()} for bot in nonebot.get_bots().values()],
                "lifecycle": {"connects": self.connect_count, "disconnects": self.disconnect_count}}

    def started(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.phase = "running"


runtime = RuntimeState()
