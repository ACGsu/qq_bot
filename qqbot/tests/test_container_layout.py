"""Container path regressions; no Docker daemon, credentials or QQ connection."""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from infrastructure.plugin_config import get_plugin_config_path
from webui.app import create_app
from webui.config_service import ConfigService
from webui.docker_service import DockerFailure, DockerService
from webui.settings import Settings


class ContainerLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "qqbot" / "config" / "plugin_config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps({"enabled_plugins": ["basic"], "plugin_settings": {}}), encoding="utf-8")
        self.runtime = self.root / "qqbot" / "storage" / "webui"

    def test_explicit_paths_use_shared_config_and_writable_runtime(self):
        config = ConfigService(self.root, config_path=self.config_path, runtime_dir=self.runtime)
        self.assertEqual(config.read()["enabled_plugins"], ["basic"])
        self.assertEqual(Path(config.lock.lock_file).parent, self.runtime)
        self.assertEqual(Path(DockerService(config).operation_lock.lock_file).parent, self.runtime)
        self.assertFalse((self.root / "plugin_config.json").exists())

    def test_settings_read_project_credentials_and_keep_public_origin_check(self):
        (self.root / ".webui.env").write_text("WEBUI_PASSWORD='literal-${NO_EXPANSION}-password'\n", encoding="utf-8")
        values = {"WEBUI_PROJECT_ROOT": str(self.root), "WEBUI_CONFIG_PATH": str(self.config_path),
                  "WEBUI_RUNTIME_DIR": str(self.runtime), "WEBUI_CONTAINER": "1",
                  "WEBUI_HOST": "0.0.0.0", "WEBUI_PORT": "8765", "WEBUI_PUBLIC_PORT": "18765"}
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.load()
        self.assertEqual(settings.password, "literal-${NO_EXPANSION}-password")
        self.assertEqual(settings.root, self.root)
        self.assertTrue(settings.container_mode)
        self.assertEqual(settings.host, "0.0.0.0")
        app = create_app(settings, runner=lambda *args: (0, ""))
        with TestClient(app, base_url="http://127.0.0.1:18765") as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/", headers={"Host": "untrusted.example:18765"}).status_code, 403)
        self.assertEqual(app.state.config.path, self.config_path)

    def test_compose_uses_engine_host_path_and_original_project_from_inspect(self):
        config = ConfigService(self.root, config_path=self.config_path, runtime_dir=self.runtime, container_mode=True)
        calls = []
        def runner(argv, cwd, timeout, **kwargs):
            calls.append((argv, kwargs))
            if argv[:2] == ["docker", "inspect"]:
                return 0, json.dumps({"root": "/run/desktop/mnt/host/d/qq_bot", "project": "qq_bot"})
            return 0, "[]"
        service = DockerService(config, runner=runner)
        with patch.dict(os.environ, {"HOSTNAME": "webui-id", "COMPOSE_PROJECT_NAME": "wrong"}, clear=True):
            service.run(["ps", "--all", "--format", "json", "qq-bot"])
        self.assertEqual(calls[0][0][-1], "webui-id")
        self.assertEqual(calls[1][0][5], str(self.root / "compose.yaml"))
        self.assertEqual(calls[1][1]["env"]["HOST_PROJECT_ROOT"], "/run/desktop/mnt/host/d/qq_bot")
        self.assertEqual(calls[1][1]["env"]["COMPOSE_PROJECT_NAME"], "qq_bot")

    def test_unknown_host_mapping_never_calls_compose(self):
        config = ConfigService(self.root, config_path=self.config_path, runtime_dir=self.runtime, container_mode=True)
        for output in ('{}', '{"root":null,"project":"qq_bot"}', 'invalid'):
            calls = []
            def runner(argv, cwd, timeout):
                calls.append(argv)
                return 0, output
            with patch.dict(os.environ, {"HOSTNAME": "webui-id"}, clear=True):
                with self.assertRaises(DockerFailure):
                    DockerService(config, runner=runner).run(["ps"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:2], ["docker", "inspect"])

    def test_bot_configuration_default_is_in_config_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_plugin_config_path(), Path(__file__).resolve().parents[1] / "config" / "plugin_config.json")


if __name__ == "__main__":
    unittest.main()

