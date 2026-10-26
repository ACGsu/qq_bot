"""Configuration and help stay privacy-preserving until sharing is explicitly enabled."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none", command_start={"/"})

from infrastructure.plugin_config import load_plugin_config, save_enabled_plugin_ids, save_plugin_config
from services.help import HELP_LINES_BY_PLUGIN, build_help_card, format_help_topic, get_help_sections
from plugins.timetable import TimetablePlugin


class TimetableSettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def plugin(self, settings=None):
        plugin = TimetablePlugin(settings, database_path=self.directory / "test.sqlite3", images_enabled=False)
        self.addCleanup(plugin.close)
        return plugin

    def test_missing_or_invalid_setting_defaults_to_isolation(self):
        for settings in (None, {}, {"group_isolation_enabled": None}, {"group_isolation_enabled": "invalid"},
                         {"group_isolation_enabled": ""}, {"group_isolation_enabled": []}):
            with self.subTest(settings=settings):
                plugin = self.plugin(settings)
                self.assertTrue(plugin.group_isolation_enabled)
                self.assertTrue(plugin.store.group_isolation_enabled)
                self.assertIn("群聊隔离：开启", plugin.scope_description)
        self.assertFalse((self.directory / "test.sqlite3").exists())

    def test_boolean_and_string_values_configure_the_same_store_mode(self):
        for value in (False, "false", "False", "0", "off", " NO "):
            with self.subTest(value=value):
                plugin = self.plugin({"group_isolation_enabled": value})
                self.assertFalse(plugin.group_isolation_enabled)
                self.assertFalse(plugin.store.group_isolation_enabled)
        for value in (True, "true", "True", "1", "on", " YES "):
            with self.subTest(value=value):
                self.assertTrue(self.plugin({"group_isolation_enabled": value}).store.group_isolation_enabled)

    def test_setting_round_trips_and_survives_parent_plugin_toggles(self):
        path = self.directory / "plugins.json"
        for value in ("true", "false"):
            with self.subTest(value=value):
                save_plugin_config(("timetable", "basic"), {
                    "timetable": {"group_isolation_enabled": value},
                    "daily_wife": {"marriage_enabled": "false"},
                }, path)
                config = load_plugin_config(path)
                self.assertEqual(config.plugin_settings["timetable"]["group_isolation_enabled"], value)
                save_enabled_plugin_ids(("basic",), path)
                self.assertNotIn("timetable", load_plugin_config(path).enabled_plugin_ids)
                self.assertEqual(load_plugin_config(path).plugin_settings["timetable"]["group_isolation_enabled"], value)
                save_enabled_plugin_ids(("timetable", "basic"), path)
                self.assertEqual(load_plugin_config(path).plugin_settings["daily_wife"]["marriage_enabled"], "false")

    def test_text_and_card_help_show_effective_mode_without_mutating_shared_content(self):
        original = HELP_LINES_BY_PLUGIN["timetable"]
        for value, label in (("true", "群聊隔离已开启"), ("false", "群聊隔离已关闭")):
            settings = {"timetable": {"group_isolation_enabled": value}}
            with self.subTest(value=value):
                self.assertIn(label, format_help_topic("课表", ("timetable",), settings))
                self.assertIn(label, json.dumps(build_help_card("999", ("timetable",), settings), ensure_ascii=False))
                self.assertEqual(HELP_LINES_BY_PLUGIN["timetable"], original)
                self.assertEqual(get_help_sections(("timetable",), settings)[0].lines[:-1], original)
        self.assertIn("群聊隔离已开启", format_help_topic("课表", ("timetable",)))
        self.assertNotIn("群聊隔离已关闭", format_help_topic("课表", ("basic",), {
            "timetable": {"group_isolation_enabled": "false"},
        }))


if __name__ == "__main__":
    unittest.main()

