"""WebUI emoji conversion reaches the native OneBot action unchanged."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from plugins.auto_emoji import handle_auto_emoji
from webui.emoji_codec import encode


class EmojiActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_emoji_ids_reach_existing_onebot_action_unchanged(self):
        ids = encode("😀👍❤️🔥")
        bot = AsyncMock()
        config = SimpleNamespace(auto_emoji_ids=",".join(ids))
        with patch("plugins.auto_emoji.get_plugin_config", return_value=config):
            await handle_auto_emoji(bot, SimpleNamespace(message_id=42))
        self.assertEqual(bot.call_api.await_count, 4)
        self.assertEqual([c.kwargs["emoji_id"] for c in bot.call_api.await_args_list], [128512, 128077, 10084, 128293])
        self.assertTrue(all(c.args == ("set_msg_emoji_like",) and c.kwargs["message_id"] == 42
                            for c in bot.call_api.await_args_list))

