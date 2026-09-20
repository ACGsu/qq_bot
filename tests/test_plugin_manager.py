import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from plugin_control import PLUGIN_DEFINITIONS, load_plugin_config, save_plugin_config

try:
    import tkinter as tk
    from plugin_manager import PluginManagerApp
except ImportError:
    tk = None


@unittest.skipIf(tk is None, "Tkinter is not installed; the GUI runs on the host, not in Docker")
class PluginManagerSettingsTest(unittest.TestCase):
    def setUp(self):
        try:
            interpreter = tk.Tcl()
        except tk.TclError as exc:
            self.skipTest(f"Tcl runtime unavailable: {exc}")
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # Use real Tcl variables without opening a window or starting Docker.
        self.app = PluginManagerApp.__new__(PluginManagerApp)
        self.app.tk = interpreter.tk
        self.app.master = None
        self.app.config_path = Path(self.temp_dir.name) / "plugins.json"
        self.app.plugin_vars = {plugin.plugin_id: tk.BooleanVar(master=interpreter, value=True) for plugin in PLUGIN_DEFINITIONS}
        self.app.setting_vars = {
            "daily_wife": {"marriage_enabled": tk.BooleanVar(master=interpreter, value=True)},
            "timetable": {"group_isolation_enabled": tk.BooleanVar(master=interpreter, value=True)},
            "auto_emoji": {"target_qq": tk.StringVar(master=interpreter, value=" 111 ")},
        }
        self.app.status_var = tk.StringVar(master=interpreter)
        self.app.enabled_summary_var = tk.StringVar(master=interpreter)
        self.app.rows_frame = MagicMock()

    def test_legacy_config_defaults_marriage_to_enabled(self):
        save_plugin_config(("daily_wife", "basic"), {}, self.app.config_path)
        self.app.setting_vars["daily_wife"]["marriage_enabled"].set(False)
        self.app._load_config()
        self.assertTrue(self.app.setting_vars["daily_wife"]["marriage_enabled"].get())
        self.assertTrue(self.app.plugin_vars["daily_wife"].get())

    def test_loads_boolean_and_string_checkbox_values(self):
        for value, expected in ((False, False), ("false", False), ("False", False), ("0", False), (True, True), ("true", True)):
            with self.subTest(value=value):
                payload = {"enabled_plugins": ["daily_wife"], "plugin_settings": {"daily_wife": {"marriage_enabled": value}}}
                self.app.config_path.write_text(json.dumps(payload), encoding="utf-8")
                self.app._load_config()
                self.assertEqual(self.app.setting_vars["daily_wife"]["marriage_enabled"].get(), expected)

    def test_checkbox_save_and_reload_preserves_independent_setting(self):
        marriage_var = self.app.setting_vars["daily_wife"]["marriage_enabled"]
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                marriage_var.set(enabled)
                self.assertTrue(self.app._save_config())
                config = load_plugin_config(self.app.config_path)
                self.assertIn("daily_wife", config.enabled_plugin_ids)
                self.assertEqual(config.plugin_settings["daily_wife"]["marriage_enabled"], "true" if enabled else "false")
                self.assertEqual(config.plugin_settings["auto_emoji"]["target_qq"], "111")
                marriage_var.set(not enabled)
                self.app._load_config()
                self.assertEqual(marriage_var.get(), enabled)

    def test_child_checkbox_is_bound_and_disabled_with_parent_without_losing_preference(self):
        parent_switch, marriage_switch = MagicMock(), MagicMock()
        with patch("plugin_manager.tk.Frame"), patch("plugin_manager.ttk.Frame"), patch("plugin_manager.ttk.Label"), patch(
            "plugin_manager.ttk.Checkbutton", side_effect=(parent_switch, marriage_switch)
        ) as checkbutton:
            self.app._add_plugin_row("daily_wife", 0)
        marriage_var = self.app.setting_vars["daily_wife"]["marriage_enabled"]
        self.assertEqual(checkbutton.call_args.kwargs["text"], "启用结芬子功能")
        self.assertIs(checkbutton.call_args.kwargs["variable"], marriage_var)
        marriage_var.set(False)
        self.app.plugin_vars["daily_wife"].set(False)
        marriage_switch.state.assert_called_with(["disabled"])
        self.app.plugin_vars["daily_wife"].set(True)
        marriage_switch.state.assert_called_with(["!disabled"])
        self.assertFalse(marriage_var.get())

    def test_parent_bulk_toggles_keep_subfeature_preference(self):
        marriage_var = self.app.setting_vars["daily_wife"]["marriage_enabled"]
        marriage_var.set(False)
        isolation_var = self.app.setting_vars["timetable"]["group_isolation_enabled"]
        isolation_var.set(False)
        self.app._disable_all()
        self.assertFalse(self.app.plugin_vars["daily_wife"].get())
        self.app._enable_all()
        self.assertTrue(self.app.plugin_vars["daily_wife"].get())
        self.assertFalse(marriage_var.get())
        self.assertFalse(isolation_var.get())

    def test_save_and_apply_saves_checkbox_before_recreating_bot(self):
        self.app.setting_vars["daily_wife"]["marriage_enabled"].set(False)
        self.app.setting_vars["timetable"]["group_isolation_enabled"].set(False)
        with patch.object(self.app, "_run_docker_command") as docker:
            self.app._save_and_restart()
        self.assertEqual(load_plugin_config(self.app.config_path).plugin_settings["daily_wife"]["marriage_enabled"], "false")
        self.assertEqual(load_plugin_config(self.app.config_path).plugin_settings["timetable"]["group_isolation_enabled"], "false")
        docker.assert_called_once_with(("up", "-d", "--build", "--force-recreate", "qq-bot"))


    def test_legacy_config_defaults_timetable_isolation_to_enabled(self):
        save_plugin_config(("timetable", "basic"), {}, self.app.config_path)
        isolation_var = self.app.setting_vars["timetable"]["group_isolation_enabled"]
        isolation_var.set(False)
        self.app._load_config()
        self.assertTrue(isolation_var.get())
        self.assertTrue(self.app.plugin_vars["timetable"].get())

    def test_timetable_isolation_checkbox_load_save_and_reload(self):
        isolation_var = self.app.setting_vars["timetable"]["group_isolation_enabled"]
        for value, expected in ((False, False), ("false", False), ("False", False), ("0", False),
                                (True, True), ("true", True), ("invalid", True)):
            with self.subTest(value=value):
                payload = {"enabled_plugins": ["timetable", "daily_wife"], "plugin_settings": {
                    "timetable": {"group_isolation_enabled": value},
                    "daily_wife": {"marriage_enabled": "false"},
                }}
                self.app.config_path.write_text(json.dumps(payload), encoding="utf-8")
                self.app._load_config()
                self.assertIs(isolation_var.get(), expected)
                self.assertTrue(self.app._save_config())
                settings = load_plugin_config(self.app.config_path).plugin_settings
                self.assertEqual(settings["timetable"]["group_isolation_enabled"], "true" if expected else "false")
                self.assertEqual(settings["daily_wife"]["marriage_enabled"], "false")
                isolation_var.set(not expected)
                self.app._load_config()
                self.assertIs(isolation_var.get(), expected)

    def test_timetable_child_checkbox_is_bound_and_disabled_without_losing_preference(self):
        parent_switch, isolation_switch = MagicMock(), MagicMock()
        with patch("plugin_manager.tk.Frame"), patch("plugin_manager.ttk.Frame"), patch(
            "plugin_manager.ttk.Label"
        ) as label, patch("plugin_manager.ttk.Checkbutton", side_effect=(parent_switch, isolation_switch)) as checkbutton:
            self.app._add_plugin_row("timetable", 0)
        isolation_var = self.app.setting_vars["timetable"]["group_isolation_enabled"]
        self.assertTrue(isolation_var.get())
        self.assertEqual(checkbutton.call_args.kwargs["text"], "启用群聊隔离")
        self.assertIs(checkbutton.call_args.kwargs["variable"], isolation_var)
        self.assertIn("其他共同群成员", label.call_args.kwargs["text"])
        isolation_var.set(False)
        self.app.plugin_vars["timetable"].set(False)
        isolation_switch.state.assert_called_with(["disabled"])
        self.app.plugin_vars["timetable"].set(True)
        isolation_switch.state.assert_called_with(["!disabled"])
        self.assertFalse(isolation_var.get())


if __name__ == "__main__":
    unittest.main()
