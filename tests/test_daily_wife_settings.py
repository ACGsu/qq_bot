import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import BotConfig, NapCatBot
from plugin_control import PluginConfig, load_plugin_config, parse_bool_setting, save_plugin_config
from plugins.basic import dispatch_command
from plugins.common import CommandContext, today_date_key
from plugins.courtship import COURTSHIP_REJECT_MESSAGE, COURTSHIP_SUCCESS_MESSAGE
from plugins.daily_wife import (
    DAILY_WIFE_COMMANDS,
    MARRIAGE_ACCEPT_COMMANDS,
    MARRIAGE_REJECT_COMMANDS,
    MARRIAGE_SUCCESS_MESSAGE,
    DailyWifePlugin,
    DailyWifeRecord,
    format_daily_wife_existing,
    format_daily_wife_new,
)


MARRIAGE_COMMANDS = {"/结芬"} | MARRIAGE_ACCEPT_COMMANDS | MARRIAGE_REJECT_COMMANDS


def message_text(message):
    if isinstance(message, str):
        return message
    return "".join(segment["data"].get("text", "") for segment in message)


def group_event(command, user_id=111, target_id=None):
    message = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": f" {command} "}},
    ]
    if target_id is not None:
        message.append({"type": "at", "data": {"qq": str(target_id)}})
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 100,
        "user_id": user_id,
        "self_id": 999,
        "message": message,
    }


def make_bot():
    return NapCatBot(BotConfig("server", "ws://example.invalid", None, "999", "127.0.0.1", 8080, 5.0, None))


class DailyWifeSettingsTest(unittest.TestCase):
    def test_boolean_settings_accept_json_values_and_strings(self):
        for value in (True, 1, "true", "True", "  YES  ", "on", "1"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool_setting(value))
        for value in (False, 0, "false", "False", "  NO  ", "off", "0"):
            with self.subTest(value=value):
                self.assertFalse(parse_bool_setting(value, default=True))

    def test_missing_or_invalid_boolean_uses_requested_default(self):
        for value in (None, "", "invalid"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool_setting(value, default=True))
                self.assertFalse(parse_bool_setting(value, default=False))

    def test_saved_marriage_setting_round_trips_without_disabling_daily_wife(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plugins.json"
            for value in ("false", "true"):
                with self.subTest(value=value):
                    save_plugin_config(("daily_wife", "basic"), {"daily_wife": {"marriage_enabled": value}}, path)
                    config = load_plugin_config(path)
                    self.assertIn("daily_wife", config.enabled_plugin_ids)
                    self.assertEqual(config.plugin_settings["daily_wife"]["marriage_enabled"], value)
                    self.assertEqual(DailyWifePlugin(config.plugin_settings["daily_wife"]).marriage_enabled, value == "true")

    def test_json_boolean_false_disables_marriage(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plugins.json"
            path.write_text(json.dumps({"plugin_settings": {"daily_wife": {"marriage_enabled": False}}}), encoding="utf-8")
            config = load_plugin_config(path)
            self.assertFalse(DailyWifePlugin(config.plugin_settings["daily_wife"]).marriage_enabled)

    def test_default_and_explicitly_enabled_marriage_match_all_commands(self):
        for settings in (None, {}, {"marriage_enabled": "true"}):
            plugin = DailyWifePlugin(settings)
            self.assertTrue(plugin.marriage_enabled)
            for command in DAILY_WIFE_COMMANDS | MARRIAGE_COMMANDS:
                with self.subTest(settings=settings, command=command):
                    self.assertTrue(plugin.matches(command, CommandContext(command, [])))
            self.assertFalse(plugin.matches("/unknown", CommandContext("/unknown", [])))

    def test_disabled_marriage_only_matches_daily_draw_commands(self):
        plugin = DailyWifePlugin({"marriage_enabled": "false"})
        for command in DAILY_WIFE_COMMANDS | MARRIAGE_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(plugin.matches(command, CommandContext(command, [])), command in DAILY_WIFE_COMMANDS)

    def test_disabled_marriage_hides_both_new_and_cached_result_hints(self):
        record = DailyWifeRecord(today_date_key(), "222", "Nick")
        for formatter in (format_daily_wife_new, format_daily_wife_existing):
            with self.subTest(formatter=formatter.__name__):
                result = formatter(record, marriage_enabled=False)
                self.assertIn("Nick", message_text(result))
                self.assertNotIn("结芬", message_text(result))
                self.assertEqual([segment["data"]["qq"] for segment in result if segment["type"] == "at"], ["222"])

    def test_help_hides_disabled_marriage_but_keeps_daily_draw_and_courtship(self):
        help_text = dispatch_command("/help", ("daily_wife", "courtship", "basic"), {"daily_wife": {"marriage_enabled": "false"}})
        for command in ("/今日群友", "/今日老婆", "/求偶", "/hello"):
            self.assertIn(command, help_text)
        for command in ("/结芬", "/愿意", "/不愿意"):
            self.assertNotIn(command, help_text)

    def test_help_keeps_marriage_for_legacy_and_enabled_settings(self):
        for settings in (None, {}, {"daily_wife": {"marriage_enabled": "true"}}):
            with self.subTest(settings=settings):
                help_text = dispatch_command("/help", ("daily_wife", "basic"), settings)
                self.assertIn("/结芬", help_text)
                self.assertIn("/愿意", help_text)

    def test_disabled_parent_hides_marriage_even_when_subfeature_enabled(self):
        help_text = dispatch_command("/help", ("basic",), {"daily_wife": {"marriage_enabled": "true"}})
        self.assertNotIn("/今日群友", help_text)
        self.assertNotIn("/今日老婆", help_text)
        self.assertNotIn("/结芬", help_text)


class DailyWifeSettingsIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_marriage_still_draws_and_reuses_cache_across_aliases(self):
        plugin = DailyWifePlugin({"marriage_enabled": "false"})
        fake_bot = SimpleNamespace(
            _send_reply=AsyncMock(),
            _get_group_member_list=AsyncMock(return_value=[{"user_id": 111}, {"user_id": 222, "nickname": "Nick"}]),
        )
        for command in ("/今日老婆", "/今日群友"):
            await plugin.handle(fake_bot, None, group_event(command), CommandContext(command, []))
        fake_bot._get_group_member_list.assert_awaited_once_with(None, "100")
        self.assertEqual(fake_bot._send_reply.await_count, 2)
        for reply in fake_bot._send_reply.await_args_list:
            self.assertIn("Nick", message_text(reply.args[2]))
            self.assertNotIn("结芬", message_text(reply.args[2]))
        self.assertEqual(plugin.daily_wives[("100", "111")].target_id, "222")
        self.assertFalse(plugin.marriage_proposals_by_requester)

    async def test_disabled_marriage_handlers_do_not_reply_or_create_timeout_tasks(self):
        plugin = DailyWifePlugin({"marriage_enabled": "false"})
        plugin.daily_wives[("100", "111")] = DailyWifeRecord(today_date_key(), "222", "Nick")
        fake_bot = SimpleNamespace(_send_reply=AsyncMock())
        with patch("plugins.daily_wife.asyncio.create_task") as create_task:
            for command in MARRIAGE_COMMANDS:
                await plugin.handle(fake_bot, None, group_event(command), CommandContext(command, []))
        fake_bot._send_reply.assert_not_awaited()
        create_task.assert_not_called()
        self.assertFalse(plugin.marriage_proposals_by_requester)
        self.assertFalse(plugin.marriage_proposals_by_target)
        self.assertFalse(plugin.marriage_results)

    async def test_saved_settings_reach_bot_routing_and_help(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plugins.json"
            save_plugin_config(("daily_wife", "basic"), {"daily_wife": {"marriage_enabled": "false"}}, path)
            with patch.dict(os.environ, {"BOT_PLUGIN_CONFIG": str(path)}):
                bot = make_bot()
            daily_wife = next(plugin for plugin in bot.plugins if isinstance(plugin, DailyWifePlugin))
            self.assertFalse(daily_wife.marriage_enabled)
            with patch.object(bot, "_send_reply", new_callable=AsyncMock) as send_reply:
                await bot._handle_raw_event(None, json.dumps(group_event("/help 文本")))
                help_text = send_reply.await_args.args[2]
                self.assertIn("/今日老婆", help_text)
                self.assertNotIn("/结芬", help_text)
                await bot._handle_raw_event(None, json.dumps(group_event("/结芬")))
                self.assertIn("没有该指令", message_text(send_reply.await_args.args[2]))
            self.assertFalse(daily_wife.marriage_proposals_by_requester)
            bot.stop()

    async def test_disabling_marriage_does_not_swallow_courtship_response_aliases(self):
        config = PluginConfig(("daily_wife", "courtship", "basic"), {"daily_wife": {"marriage_enabled": "false"}})
        for command, expected in (("/接受", COURTSHIP_SUCCESS_MESSAGE), ("/拒绝", COURTSHIP_REJECT_MESSAGE)):
            with self.subTest(command=command), patch("bot.load_plugin_config", return_value=config):
                bot = make_bot()
                with patch.object(bot, "_send_reply", new_callable=AsyncMock) as send_reply:
                    await bot._handle_raw_event(None, json.dumps(group_event("/求偶", target_id=222)))
                    await bot._handle_raw_event(None, json.dumps(group_event(command, user_id=222)))
                    self.assertIn(expected, message_text(send_reply.await_args.args[2]))
                bot.stop()

    async def test_explicitly_enabled_marriage_can_be_accepted_through_bot(self):
        config = PluginConfig(("daily_wife", "basic"), {"daily_wife": {"marriage_enabled": "true"}})
        with patch("bot.load_plugin_config", return_value=config):
            bot = make_bot()
        daily_wife = next(plugin for plugin in bot.plugins if isinstance(plugin, DailyWifePlugin))
        daily_wife.daily_wives[("100", "111")] = DailyWifeRecord(today_date_key(), "222", "Nick")
        with patch.object(bot, "_send_reply", new_callable=AsyncMock) as send_reply:
            await bot._handle_raw_event(None, json.dumps(group_event("/结芬")))
            self.assertIn("/愿意", message_text(send_reply.await_args.args[2]))
            await bot._handle_raw_event(None, json.dumps(group_event("/愿意", user_id=222)))
            self.assertEqual(send_reply.await_args.args[2], MARRIAGE_SUCCESS_MESSAGE)
        self.assertFalse(daily_wife.marriage_proposals_by_requester)
        bot.stop()


if __name__ == "__main__":
    unittest.main()
