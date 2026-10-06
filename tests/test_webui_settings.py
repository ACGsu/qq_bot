import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from filelock import FileLock
import run_webui
from webui.settings import Settings
from webui_support import PASSWORD


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load(self, text):
        (self.root / ".webui.env").write_text(text, encoding="utf-8")
        with patch("webui.settings.ROOT", self.root), patch.dict("os.environ", {}, clear=True):
            return Settings.load()

    def test_password_bounds_and_invalid_port_report_validation_errors(self):
        for password in (None, "", "short", " " * 16, "x" * 513):
            with self.subTest(password_length=len(password or "")), self.assertRaises(ValueError):
                Settings(password=password)
        for port in (None, "8765", True, 1023, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                Settings(password=PASSWORD, port=port)
        for size in (16, 512):
            self.assertEqual(len(Settings(password="x" * size).password), size)

    def test_empty_bare_or_invalid_env_entries_are_safe_errors(self):
        for text in ("", "WEBUI_PASSWORD", "WEBUI_PASSWORD=", f"WEBUI_PASSWORD={PASSWORD}\nWEBUI_PORT", f"WEBUI_PASSWORD={PASSWORD}\nWEBUI_PORT=bad"):
            with self.subTest(text=text.splitlines()[-1:] or []), self.assertRaises(ValueError):
                self.load(text)

    def test_loading_does_not_read_bot_env_or_interpolate_password(self):
        (self.root / ".env").write_text(f"WEBUI_PASSWORD={PASSWORD}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load("")
        self.assertEqual(self.load("WEBUI_PASSWORD=literal-${NOT_EXPANDED}-password").password,
                         "literal-${NOT_EXPANDED}-password")
        with patch("webui.settings.ROOT", self.root), patch.dict("os.environ", {"WEBUI_PASSWORD": PASSWORD, "WEBUI_PORT": "18765"}, clear=True):
            settings = Settings.load()
        self.assertEqual((settings.password, settings.port), (PASSWORD, 18765))

    def test_entrypoint_binds_loopback_without_reload_and_releases_lock(self):
        settings = Settings(password=PASSWORD, root=self.root)
        with patch.object(run_webui.Settings, "load", return_value=settings), patch.object(run_webui, "create_app", return_value="mock-app"), patch.object(run_webui.uvicorn, "run") as serve:
            self.assertEqual(run_webui.main(), 0)
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
        self.assertFalse(serve.call_args.kwargs["proxy_headers"])
        self.assertFalse(serve.call_args.kwargs.get("reload", False))
        with FileLock(str(self.root / ".webui-server.lock"), timeout=0):
            pass

    def test_second_instance_and_io_failure_never_start_server(self):
        settings = Settings(password=PASSWORD, root=self.root)
        error = io.StringIO()
        with patch.object(run_webui.Settings, "load", return_value=settings), patch.object(run_webui.uvicorn, "run") as serve, contextlib.redirect_stderr(error):
            with FileLock(str(self.root / ".webui-server.lock"), timeout=0):
                self.assertEqual(run_webui.main(), 1)
            serve.assert_not_called()
        with patch.object(run_webui.Settings, "load", side_effect=PermissionError("private-path")), contextlib.redirect_stderr(error):
            self.assertEqual(run_webui.main(), 1)
        self.assertNotIn("private-path", error.getvalue())
