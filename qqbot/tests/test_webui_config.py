import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from infrastructure.plugin_config import atomic_write_plugin_payload, load_plugin_payload_strict
from webui.config_service import ConfigService, Conflict
from webui.schemas import ConfigEdit

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = ConfigService(self.root)
        self.payload = {"enabled_plugins":["auto_emoji","rps","daily_wife","timetable"],
          "extension": {"keep":"yes"}, "plugin_settings": {
           "auto_emoji":{"emoji_id":"99999,127852", "target_qq":"12345", "future":"kept"},
           "rps":{"ban_seconds":"120"}, "daily_wife":{"marriage_enabled":"false"},
           "timetable":{"group_isolation_enabled":"false"},
           "future_plugin":{"custom":"kept"}}}
        self.service.path.write_text(json.dumps(self.payload),encoding="utf-8")
        self.env = patch.dict(os.environ,{},clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def edit(self, **values):
        current = self.service.read()
        return ConfigEdit(version=current["version"], enabled_plugins=current["enabled_plugins"], **values)

    def raw(self):
        return json.loads(self.service.path.read_text(encoding="utf-8"))

    def test_read_hides_unknown_settings(self):
        value = self.service.read()
        self.assertNotIn("future",value["plugin_settings"]["auto_emoji"])
        self.assertNotIn("future_plugin",value["plugin_settings"])
        self.assertEqual(value["emoji"][0]["label"],"旧版表情（保留）")

    def test_disabled_plugins_preserve_children_and_unedited_values(self):
        edit = self.edit()
        edit.enabled_plugins=[]
        self.service.save(edit)
        result=self.raw()
        self.assertEqual(result["plugin_settings"],self.payload["plugin_settings"])
        self.assertEqual(result["extension"],self.payload["extension"])
        edit=self.edit(); self.service.save(edit)
        self.assertEqual(self.raw()["plugin_settings"],self.payload["plugin_settings"])

    def test_retired_summary_is_removed_without_exposing_environment_secret(self):
        self.payload["enabled_plugins"].append("summary")
        self.payload["plugin_settings"]["summary"]={"deepseek_api_key":"private-json-key"}
        self.service.path.write_text(json.dumps(self.payload),encoding="utf-8")
        (self.root/".env").write_text("DEEPSEEK_API_KEY=environment-key",encoding="utf-8")
        value=self.service.read()
        self.assertNotIn("summary",value["enabled_plugins"])
        self.assertNotIn("summary",value["plugin_settings"])
        self.assertNotIn("environment-key",json.dumps(value))
        self.service.save(ConfigEdit(version=value["version"],enabled_plugins=value["enabled_plugins"]))
        self.assertNotIn("summary",self.raw()["enabled_plugins"])
        self.assertNotIn("summary",self.raw()["plugin_settings"])

    def test_validation_and_version_conflict(self):
        edit=self.edit(plugin_settings={"rps":{"ban_seconds":"60"}})
        before=self.service.path.read_bytes()
        self.service.save(edit,validate_only=True)
        self.assertEqual(self.service.path.read_bytes(),before)
        self.service.save(edit)
        with self.assertRaises(Conflict): self.service.save(edit)

    def test_concurrent_pages_only_one_succeeds(self):
        edit=self.edit(plugin_settings={"rps":{"ban_seconds":"240"}})
        other=ConfigService(self.root)
        def save(service):
            try: service.save(edit); return "ok"
            except Conflict: return "conflict"
        with ThreadPoolExecutor(2) as executor:
            self.assertCountEqual(list(executor.map(save,[self.service,other])),["ok","conflict"])

    def test_rps_rejects_invalid_seconds_even_when_disabled(self):
        for value in ["", "0", "-1", "1.5", "abc", "86401", " 60", "１", "1e2"]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.service.save(self.edit(plugin_settings={"rps":{"ban_seconds":value}}))
        for value in ["1","60","120","86400"]:
            self.service.save(self.edit(plugin_settings={"rps":{"ban_seconds":value}}))
            self.assertEqual(self.raw()["plugin_settings"]["rps"]["ban_seconds"],value)

    def test_corrupt_file_cannot_be_silently_overwritten(self):
        for payload in ["{bad-json", "[]", '{"enabled_plugins":true}', '{"enabled_plugins":[],"plugin_settings":{"rps":{"ban_seconds":"0"}}}']:
            self.service.path.write_text(payload,encoding="utf-8")
            with self.assertRaises(ValueError): self.service.read()
            with self.assertRaises(ValueError): self.service.save(ConfigEdit(version="bad",enabled_plugins=[]))
            self.assertEqual(self.service.path.read_text(encoding="utf-8"),payload)

    def test_replace_failure_cleans_only_own_temp_no_backup(self):
        before=self.service.path.read_bytes()
        with patch("os.replace",side_effect=OSError("simulated")):
            with self.assertRaises(OSError): self.service.save(self.edit())
        self.assertEqual(self.service.path.read_bytes(),before)
        self.assertEqual(list(self.root.glob(".webui-*.tmp")),[])
        self.assertFalse((self.root/"backups").exists())

    def test_legacy_unknown_emoji_preserved_until_explicitly_removed(self):
        self.service.save(self.edit(emoji_text="㊗️🍬",retained_emoji_ids=["99999"]))
        self.assertEqual(self.raw()["plugin_settings"]["auto_emoji"]["emoji_ids"],"99999,12951,127852")
        self.service.save(self.edit(emoji_text="🍬",retained_emoji_ids=[]))
        self.assertEqual(self.raw()["plugin_settings"]["auto_emoji"]["emoji_ids"],"127852")
        with self.assertRaises(ValueError): self.service.save(self.edit(retained_emoji_ids=["123456"]))

    def test_missing_file_defaults_without_creating_it(self):
        self.service.path.unlink()
        value=self.service.read()
        self.assertEqual(len(value["definitions"]),8)
        self.assertEqual(value["version"],"missing")
        self.assertFalse(self.service.path.exists())

    def test_url_credentials_and_unknown_fields_rejected(self):
        for settings in [
            {"rps":{"arbitrary":"x"}},
            {"summary":{"deepseek_api_base_url":"https://user:secret@example.org"}},
            {"unknown":{"value":"x"}},
        ]:
            with self.assertRaises(ValueError): self.service.save(self.edit(plugin_settings=settings))

    def test_legacy_boolean_values_remain_compatible_and_preserved(self):
        self.payload["plugin_settings"]["daily_wife"]["marriage_enabled"]=False
        self.payload["plugin_settings"]["timetable"]["group_isolation_enabled"]=True
        self.service.path.write_text(json.dumps(self.payload),encoding="utf-8")
        value=self.service.read()
        self.assertEqual(value["plugin_settings"]["daily_wife"]["marriage_enabled"],"false")
        self.service.save(self.edit(plugin_settings={"rps":{"ban_seconds":"90"}}))
        self.assertIs(self.raw()["plugin_settings"]["daily_wife"]["marriage_enabled"],False)

    def test_env_emoji_preview_does_not_materialize_in_json_when_unedited(self):
        self.payload["plugin_settings"].pop("auto_emoji")
        self.service.path.write_text(json.dumps(self.payload),encoding="utf-8")
        (self.root/".env").write_text("AUTO_EMOJI_IDS=12951,99999",encoding="utf-8")
        self.assertEqual([x["id"] for x in self.service.read()["emoji"]],["12951","99999"])
        self.service.save(self.edit())
        self.assertNotIn("auto_emoji",self.raw()["plugin_settings"])

    def test_extended_emoji_and_unknown_legacy_survive_unrelated_save(self):
        self.service.save(self.edit(emoji_text="😀❤️👍", retained_emoji_ids=["99999"]))
        expected = "99999,128512,10084,128077"
        self.assertEqual(self.raw()["plugin_settings"]["auto_emoji"]["emoji_ids"], expected)
        self.service.save(self.edit(plugin_settings={"rps": {"ban_seconds": "30"}}))
        self.assertEqual(self.raw()["plugin_settings"]["auto_emoji"]["emoji_ids"], expected)
        items = self.service.read()["emoji"]
        self.assertTrue(items[0]["legacy"])
        self.assertTrue(all(not item["legacy"] for item in items[1:]))

