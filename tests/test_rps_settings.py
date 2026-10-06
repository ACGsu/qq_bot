import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from plugins.rps import RockPaperScissorsPlugin
from plugins.common import CommandContext

class RpsSettingsTests(unittest.TestCase):
    def test_default_and_invalid_safe_fallback(self):
        self.assertEqual(RockPaperScissorsPlugin().ban_seconds,60)
        for value in ["", "0", "-1", "1.2", "86401", None, 120]:
            with self.assertLogs("qq-bot",level="WARNING") as logs:
                self.assertEqual(RockPaperScissorsPlugin({"ban_seconds":value}).ban_seconds,60)
            self.assertIn("safe default",logs.output[0])
            self.assertNotIn(str(value),logs.output[0]) if value == "86401" else None

    def run_game(self, settings, choices, event=None, target="222"):
        bot=AsyncMock()
        context=CommandContext(text="/猜拳",mentions=[target])
        with patch("plugins.rps.random.choice",side_effect=choices):
            asyncio.run(RockPaperScissorsPlugin(settings).handle(bot,None,event or {"message_type":"group","user_id":"111","group_id":"99"},context))
        return bot

    def test_duration_and_result_share_configured_value(self):
        for seconds,text in [(60,"禁言1分钟"),(120,"禁言2分钟"),(61,"禁言61秒")]:
            bot=self.run_game({"ban_seconds":str(seconds)},["石头","剪刀"])
            bot._ban_group_member.assert_awaited_once_with(None,"99","222",seconds)
            reply=bot._send_reply.call_args.args[2]
            self.assertIn(text,"".join(x["data"].get("text","") for x in reply))

    def test_tie_and_original_restrictions(self):
        bot=self.run_game({"ban_seconds":"120"},["石头","石头"])
        bot._ban_group_member.assert_not_awaited()
        bot=self.run_game({},[],target="111"); bot._ban_group_member.assert_not_awaited()
        bot=self.run_game({},[],event={"message_type":"private","user_id":"111"}); bot._ban_group_member.assert_not_awaited()

    def test_bot_factory_and_real_action_payload(self):
        from bot import NapCatBot, BotConfig
        from plugin_control import PluginConfig
        with patch("bot.load_plugin_config",return_value=PluginConfig(("rps",),{"rps":{"ban_seconds":"120"}})):
            bot=NapCatBot(BotConfig("server","ws://example.invalid",None,"999","127.0.0.1",8080,5.0,None))
        self.addCleanup(bot.stop)
        plugin=bot.plugins[0]
        self.assertEqual(plugin.ban_seconds,120)
        with patch.object(bot,"_send_action",new_callable=AsyncMock) as send, patch.object(bot,"_send_reply",new_callable=AsyncMock), patch("plugins.rps.random.choice",side_effect=["石头","剪刀"]):
            asyncio.run(plugin.handle(bot,None,{"message_type":"group","user_id":"111","group_id":"99"},CommandContext("/猜拳",["222"])))
        send.assert_awaited_once_with(None,"set_group_ban",{"group_id":99,"user_id":222,"duration":120})
