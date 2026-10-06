"""Conversion-to-action regression: fake bot, no QQ or Docker connections."""
import unittest
from unittest.mock import AsyncMock
from plugins.auto_emoji import AutoEmojiPlugin
from webui.emoji_codec import encode


class EmojiActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_emoji_ids_reach_existing_onebot_action_unchanged(self):
        ids = encode("😀👍❤️🔥")
        plugin = AutoEmojiPlugin({"target_qq": "12345", "emoji_ids": ",".join(ids)})
        bot = AsyncMock()
        await plugin.handle_event(bot, None, {"message_id": 42, "message_type": "group", "user_id": 12345})
        self.assertEqual(bot._send_action.await_count, 4)
        self.assertEqual([c.args[2]["emoji_id"] for c in bot._send_action.await_args_list], [128512, 128077, 10084, 128293])
        self.assertTrue(all(c.args[1] == "set_msg_emoji_like" and c.args[2]["message_id"] == 42 for c in bot._send_action.await_args_list))
