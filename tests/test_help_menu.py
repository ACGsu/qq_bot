import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import BotConfig, NapCatBot
from plugin_control import PLUGIN_ORDER, PluginConfig
from plugins.basic import BasicCommandPlugin, HELP_CARD_FALLBACK_PREFIX, dispatch_command
from plugins.help_menu import (
    HELP_LINES_BY_PLUGIN,
    HELP_TITLES_BY_PLUGIN,
    MARRIAGE_HELP_LINES,
    build_help_card,
    format_help,
    format_help_topic,
    get_help_sections,
)
from plugins.common import CommandContext, text_segment
from plugins.daily_wife import DailyWifePlugin
from plugins.timetable import TimetablePlugin


def walk_segments(segments, depth=0):
    for segment in segments:
        yield segment, depth
        if segment["type"] == "node":
            yield from walk_segments(segment["data"]["content"], depth + 1)


def message_text(segments):
    return "\n".join(segment["data"]["text"] for segment, _ in walk_segments(segments) if segment["type"] == "text")


def group_event(command="/help", group_id=100):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": 111,
        "self_id": 999,
        "sender": {"nickname": "requester-not-the-bot"},
        "message": [{"type": "at", "data": {"qq": "999"}}, text_segment(f" {command} ")],
    }


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.actions = asyncio.Queue()

    async def send(self, payload):
        action = json.loads(payload)
        self.sent.append(action)
        self.actions.put_nowait(action)


async def cancel_task(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class HelpContentTest(unittest.TestCase):
    def test_default_sections_follow_registry_order_and_omit_passive_plugin(self):
        expected = [plugin_id for plugin_id in PLUGIN_ORDER if plugin_id != "auto_emoji"]
        self.assertEqual([section.plugin_id for section in get_help_sections()], expected)
        self.assertEqual(set(HELP_LINES_BY_PLUGIN), set(expected))
        self.assertEqual(set(HELP_TITLES_BY_PLUGIN), set(expected))

    def test_enabled_sections_are_normalized_and_deduplicated(self):
        sections = get_help_sections(("basic", "unknown", "timetable", "basic", "auto_emoji"))
        self.assertEqual([section.plugin_id for section in sections], ["timetable", "basic"])

    def test_empty_or_passive_only_config_exposes_no_commands(self):
        for enabled in ((), ("auto_emoji",), ("unknown",)):
            with self.subTest(enabled=enabled):
                self.assertEqual(get_help_sections(enabled), ())
                self.assertEqual(format_help(enabled), "可用指令：")
                self.assertIn("暂无可用分类", format_help_topic("课表", enabled))
                card = build_help_card("999", enabled)
                self.assertEqual(card["summary"], "0 个功能分类")
                self.assertEqual(len(card["message"]), 1)
                self.assertNotIn("/导入课表", json.dumps(card, ensure_ascii=False))

    def test_existing_text_help_imports_stay_compatible(self):
        from plugins.basic import HELP_LINES_BY_PLUGIN as previous_lines
        from plugins.basic import MARRIAGE_HELP_LINES as previous_marriage_lines
        from plugins.basic import format_help as previous_formatter

        self.assertIs(previous_lines, HELP_LINES_BY_PLUGIN)
        self.assertIs(previous_marriage_lines, MARRIAGE_HELP_LINES)
        self.assertIs(previous_formatter, format_help)
        self.assertEqual(dispatch_command("/help"), format_help())

    def test_disabled_commands_are_absent_from_text_card_and_topic(self):
        enabled = ("daily_wife", "basic")
        outputs = (
            format_help(enabled),
            json.dumps(build_help_card("999", enabled), ensure_ascii=False),
            format_help_topic("今日老婆", enabled),
        )
        for output in outputs:
            self.assertIn("/今日老婆", output)
            for command in ("/课表", "/导入课表", "/song", "/猜拳", "/求偶"):
                self.assertNotIn(command, output)

    def test_marriage_setting_filters_every_help_format_and_card_preview(self):
        for value in (False, "false", "False", "0", "off", " NO "):
            with self.subTest(value=value):
                settings = {"daily_wife": {"marriage_enabled": value}}
                outputs = (
                    format_help(plugin_settings=settings),
                    format_help_topic("今日群友", plugin_settings=settings),
                    json.dumps(build_help_card("999", plugin_settings=settings), ensure_ascii=False),
                )
                for output in outputs:
                    self.assertIn("/今日老婆", output)
                    for command in ("/结芬", "/愿意", "/不愿意"):
                        self.assertNotIn(command, output)

    def test_marriage_remains_enabled_by_default_or_with_invalid_setting(self):
        for settings in (None, {}, {"daily_wife": {}}, {"daily_wife": {"marriage_enabled": "invalid"}},
                         {"daily_wife": {"marriage_enabled": "true"}}):
            with self.subTest(settings=settings):
                sections = get_help_sections(("daily_wife",), settings)
                self.assertEqual(sections[0].lines, HELP_LINES_BY_PLUGIN["daily_wife"] + MARRIAGE_HELP_LINES)
        self.assertNotIn("/结芬", format_help(("basic",), {"daily_wife": {"marriage_enabled": "true"}}))

    def test_topic_names_aliases_and_plugin_ids_select_one_section(self):
        cases = {
            "basic": ("基础指令", "基础", "帮助", "basic", "BASIC"),
            "rps": ("猜拳", "rps"),
            "daily_wife": ("今日老婆", "今日群友", "daily_wife"),
            "courtship": ("求偶", "courtship"),
            "song": ("歌曲下载", "歌曲", "点歌", "song", "/song"),
            "novel": ("小说链接", "小说", "novel"),
            "summary": ("群聊总结", "总结", "summary"),
            "timetable": ("课表", "/课表", "timetable", " TIMETABLE "),
        }
        for plugin_id, aliases in cases.items():
            expected = format_help_topic(plugin_id)
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(format_help_topic(alias), expected)
                    self.assertTrue(expected.startswith(HELP_TITLES_BY_PLUGIN[plugin_id] + "｜指令说明\n"))
                    self.assertIn(HELP_LINES_BY_PLUGIN[plugin_id][0], expected)

    def test_text_shortcuts_preserve_flat_help_and_setting_filters(self):
        enabled = ("daily_wife", "basic")
        settings = {"daily_wife": {"marriage_enabled": "false"}}
        for command in ("/help", "/help 文本", "/help 全部", "/help TEXT", "/help all", "/help   文本  "):
            with self.subTest(command=command):
                self.assertEqual(dispatch_command(command, enabled, settings), format_help(enabled, settings))

    def test_topic_dispatch_preserves_real_timetable_command_names(self):
        text = dispatch_command("/help    课表  ")
        for line in HELP_LINES_BY_PLUGIN["timetable"]:
            self.assertIn(line, text)
        self.assertNotIn("/课表导入", text)
        self.assertNotIn("/今日老婆", text)
        self.assertNotIn("/hello", text)

    def test_unknown_and_disabled_topics_only_suggest_active_categories(self):
        for topic in ("unknown", "课表", "timetable", "auto_emoji", "课表 导入"):
            with self.subTest(topic=topic):
                text = format_help_topic(topic, ("daily_wife", "basic"))
                self.assertIn("未找到该帮助分类，或该插件未启用", text)
                self.assertIn("可用分类：今日老婆、基础指令", text)
                self.assertNotIn("课表", text)
                self.assertNotIn("/结芬", text)

    def test_card_has_only_shallow_bot_authored_nodes_and_text(self):
        card = build_help_card(999)
        self.assertIn("message", card)
        self.assertNotIn("messages", card)
        self.assertNotIn("group_id", card)
        self.assertEqual(card, json.loads(json.dumps(card, ensure_ascii=False)))
        self.assertTrue(all(node["type"] == "node" for node in card["message"]))
        for segment, depth in walk_segments(card["message"]):
            self.assertLessEqual(depth, 2)
            self.assertIn(segment["type"], ("node", "text"))
            if segment["type"] == "node":
                self.assertEqual(segment["data"]["user_id"], "999")
                self.assertNotIn("id", segment["data"])
                self.assertTrue(segment["data"]["content"])
            else:
                self.assertIsInstance(segment["data"]["text"], str)

    def test_card_categories_and_leaves_share_exact_text_help_content(self):
        sections = get_help_sections()
        card = build_help_card("999")
        self.assertEqual(len(card["message"]), len(sections) + 1)
        self.assertEqual(card["summary"], f"{len(sections)} 个功能分类")
        for section, category in zip(sections, card["message"][1:]):
            data = category["data"]
            self.assertEqual(data["nickname"], section.title)
            self.assertEqual(data["source"], f"{section.title}｜帮助分类")
            self.assertEqual(data["summary"], f"{len(section.lines)} 条指令说明")
            self.assertTrue(all(node["type"] == "node" for node in data["content"]))
            leaves = [node["data"]["content"][0]["data"]["text"] for node in data["content"]]
            self.assertEqual(tuple(leaves), section.lines)
        self.assertEqual(format_help(), "可用指令：\n" + "\n".join(line for section in sections for line in section.lines))

    def test_home_preview_is_categories_not_a_flat_command_dump(self):
        card = build_help_card("999", ("daily_wife", "timetable", "basic"))
        self.assertEqual([preview["text"] for preview in card["news"]], ["今日老婆", "课表", "基础指令"])
        intro = message_text([card["message"][0]])
        for instruction in ("点击", "返回按钮", "不会自动执行", "真正 @bot", "/help 文本", "/help 分类名"):
            self.assertIn(instruction, intro)
        self.assertNotIn("/导入课表", intro)
        self.assertLessEqual(len(build_help_card("999")["news"]), 4)

    def test_invalid_sender_is_rejected_instead_of_impersonating_a_member(self):
        for value in (None, "", " ", 0, "0", -1, "-999", True, False, 999.0, "all", "９９９", "[CQ:at,qq=111]"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_help_card(value)
        self.assertEqual(build_help_card(" 999 ")["message"][0]["data"]["user_id"], "999")

    def test_cards_are_fresh_snapshots_without_mutating_shared_help(self):
        enabled = ("daily_wife", "basic")
        settings = {"daily_wife": {"marriage_enabled": "true"}}
        old_card = build_help_card("999", enabled, settings)
        settings["daily_wife"]["marriage_enabled"] = "false"
        new_card = build_help_card("999", enabled, settings)
        self.assertIn("/结芬", json.dumps(old_card, ensure_ascii=False))
        self.assertNotIn("/结芬", json.dumps(new_card, ensure_ascii=False))
        old_card["message"][1]["data"]["content"].clear()
        old_card["news"].clear()
        self.assertEqual(new_card, build_help_card("999", enabled, settings))
        self.assertEqual(len(HELP_LINES_BY_PLUGIN["daily_wife"]), 1)


class HelpRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"TIMETABLE_DB_PATH": str(Path(temp_dir) / "timetable.sqlite3")}))

    def make_bot(self, enabled=("daily_wife", "timetable", "basic"), settings=None, bot_qq="999"):
        config = BotConfig("server", "ws://example.invalid", None, bot_qq, "127.0.0.1", 8080, 5.0, None)
        with patch("bot.load_plugin_config", return_value=PluginConfig(enabled, settings or {})):
            bot = NapCatBot(config)
        self.addCleanup(bot.stop)
        return bot

    async def start_card_request(self, bot, websocket=None, event=None):
        websocket = websocket or FakeWebSocket()
        task = asyncio.create_task(bot._handle_raw_event(websocket, json.dumps(event or group_event())))
        self.addAsyncCleanup(cancel_task, task)
        request = await asyncio.wait_for(websocket.actions.get(), 1)
        self.assertEqual(request["action"], "send_group_forward_msg")
        return websocket, task, request

    async def finish_card_request(self, bot, websocket, task, request, response):
        await bot._handle_raw_event(websocket, json.dumps({**response, "echo": request["echo"]}))
        await asyncio.wait_for(task, 1)

    def assert_text_reply(self, websocket, expected):
        self.assertEqual([item["action"] for item in websocket.sent], ["send_msg"])
        self.assertEqual(message_text(websocket.sent[0]["params"]["message"]), expected)

    def assert_fallback(self, bot, websocket):
        self.assertEqual([item["action"] for item in websocket.sent], ["send_group_forward_msg", "send_msg"])
        fallback = websocket.sent[-1]["params"]
        self.assertEqual(fallback["message_type"], "group")
        self.assertEqual(fallback["group_id"], 100)
        self.assertEqual(message_text(fallback["message"]), HELP_CARD_FALLBACK_PREFIX + format_help(bot.enabled_plugin_ids, bot.plugin_settings))
        self.assertEqual(bot._action_waiters, {})

    async def test_group_help_sends_one_card_without_readback_or_business_actions(self):
        bot = self.make_bot()
        websocket, task, request = await self.start_card_request(bot)
        expected = build_help_card("999", bot.enabled_plugin_ids, bot.plugin_settings)
        expected["group_id"] = "100"
        self.assertEqual(request["params"], expected)
        self.assertIn(request["echo"], bot._action_waiters)
        await self.finish_card_request(bot, websocket, task, request, {
            "status": "ok", "retcode": 0, "data": {"message_id": 123},
        })
        # No get_forward_msg: an empty nested readback does not mean QQ failed to render it.
        self.assertEqual([item["action"] for item in websocket.sent], ["send_group_forward_msg"])
        self.assertEqual(bot._action_waiters, {})
        for plugin in bot.plugins:
            if isinstance(plugin, DailyWifePlugin):
                self.assertEqual(plugin.daily_wives, {})
                self.assertEqual(plugin.marriage_proposals_by_requester, {})
            if isinstance(plugin, TimetablePlugin):
                self.assertEqual(plugin.sessions, {})
                self.assertFalse(plugin.store.path.exists())

    async def test_group_text_shortcuts_do_not_send_cards(self):
        bot = self.make_bot()
        for command in ("/help 文本", "/help 全部", "/help text", "/help ALL"):
            with self.subTest(command=command):
                websocket = FakeWebSocket()
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event(command))), 1)
                self.assert_text_reply(websocket, format_help(bot.enabled_plugin_ids, bot.plugin_settings))
                self.assertEqual(websocket.sent[0]["params"]["group_id"], 100)

    async def test_category_help_uses_text_without_running_the_category_commands(self):
        bot = self.make_bot()
        for command in ("/help 课表", "/help /timetable", "/help 今日老婆", "/help 基础"):
            with self.subTest(command=command):
                websocket = FakeWebSocket()
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event(command))), 1)
                self.assert_text_reply(websocket, dispatch_command(command, bot.enabled_plugin_ids, bot.plugin_settings))
        timetable = next(plugin for plugin in bot.plugins if isinstance(plugin, TimetablePlugin))
        self.assertEqual(timetable.sessions, {})
        self.assertFalse(timetable.store.path.exists())

    async def test_disabled_category_uses_text_suggestions(self):
        bot = self.make_bot(enabled=("daily_wife", "basic"))
        websocket = FakeWebSocket()
        await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event("/help 课表"))), 1)
        self.assert_text_reply(websocket, format_help_topic("课表", bot.enabled_plugin_ids))
        self.assertNotIn("课表", message_text(websocket.sent[0]["params"]["message"]))

    async def test_private_help_keeps_text_and_private_destination(self):
        bot = self.make_bot()
        for command in ("/help", "/help 课表"):
            with self.subTest(command=command):
                event = group_event(command)
                event["message_type"] = "private"
                event.pop("group_id")
                websocket = FakeWebSocket()
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(event)), 1)
                self.assert_text_reply(websocket, dispatch_command(command, bot.enabled_plugin_ids, bot.plugin_settings))
                params = websocket.sent[0]["params"]
                self.assertEqual(params["message_type"], "private")
                self.assertEqual(params["user_id"], 111)
                self.assertNotIn("group_id", params)

    async def test_help_still_requires_a_genuine_bot_mention(self):
        bot = self.make_bot()
        for message_type in ("group", "private"):
            for message in (
                "/help", "@bot /help", [text_segment("/help")],
                [{"type": "at", "data": {"qq": "888"}}, text_segment(" /help")],
            ):
                with self.subTest(message_type=message_type, message=message):
                    event = group_event()
                    event.update(message_type=message_type, message=message)
                    websocket = FakeWebSocket()
                    await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(event)), 1)
                    self.assertEqual(websocket.sent, [])

    async def test_hello_is_unchanged(self):
        bot = self.make_bot()
        websocket = FakeWebSocket()
        await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event("/hello"))), 1)
        self.assert_text_reply(websocket, "啦啦啦")

    async def test_disabling_basic_still_disables_all_help_routes(self):
        bot = self.make_bot(enabled=("daily_wife",))
        for command in ("/help", "/help 文本", "/help 今日老婆"):
            with self.subTest(command=command):
                websocket = FakeWebSocket()
                await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event(command))), 1)
                self.assertEqual([item["action"] for item in websocket.sent], ["send_msg"])
                self.assertIn("没有该指令", message_text(websocket.sent[0]["params"]["message"]))

    async def test_event_self_id_is_used_instead_of_requester_or_stale_config(self):
        bot = self.make_bot()
        event = group_event()
        event["self_id"] = 888
        websocket, task, request = await self.start_card_request(bot, event=event)
        for segment, _ in walk_segments(request["params"]["message"]):
            if segment["type"] == "node":
                self.assertEqual(segment["data"]["user_id"], "888")
        self.assertNotIn("requester-not-the-bot", json.dumps(request["params"]))
        await self.finish_card_request(bot, websocket, task, request, {"status": "ok", "data": {"message_id": 123}})

    async def test_configured_bot_id_is_used_when_event_self_id_is_missing(self):
        bot = self.make_bot()
        event = group_event()
        event.pop("self_id")
        websocket, task, request = await self.start_card_request(bot, event=event)
        self.assertEqual(request["params"]["message"][0]["data"]["user_id"], "999")
        await self.finish_card_request(bot, websocket, task, request, {"status": "ok", "data": {"message_id": 123}})

    async def test_cq_mention_whitespace_and_auto_detected_bot_id_work(self):
        bot = self.make_bot(bot_qq=None)
        event = group_event()
        event["message"] = "[CQ:at,qq=999]   /help   "
        websocket, task, request = await self.start_card_request(bot, event=event)
        self.assertEqual(request["params"]["message"][0]["data"]["user_id"], "999")
        await self.finish_card_request(bot, websocket, task, request, {"status": "ok", "data": {"message_id": 123}})

    async def test_missing_or_invalid_bot_identity_falls_back_without_sending_a_card(self):
        for self_id in (None, "invalid", "all", 0):
            with self.subTest(self_id=self_id):
                bot = SimpleNamespace(
                    config=SimpleNamespace(bot_qq=None), enabled_plugin_ids=("basic",), plugin_settings={},
                    _send_action_request=AsyncMock(), _send_reply=AsyncMock(),
                )
                event = group_event()
                event["self_id"] = self_id
                with self.assertLogs("qq-bot", level="WARNING"):
                    await BasicCommandPlugin().handle(bot, None, event, CommandContext("/help", []))
                bot._send_action_request.assert_not_awaited()
                bot._send_reply.assert_awaited_once_with(None, event, HELP_CARD_FALLBACK_PREFIX + format_help(("basic",)))

    async def test_disabled_marriage_is_also_hidden_in_the_sent_card(self):
        bot = self.make_bot(settings={"daily_wife": {"marriage_enabled": "false"}})
        daily_wife = next(plugin for plugin in bot.plugins if isinstance(plugin, DailyWifePlugin))
        self.assertFalse(daily_wife.marriage_enabled)
        websocket, task, request = await self.start_card_request(bot)
        payload_text = json.dumps(request["params"], ensure_ascii=False)
        self.assertIn("/今日老婆", payload_text)
        for command in ("/结芬", "/愿意", "/不愿意"):
            self.assertNotIn(command, payload_text)
        await self.finish_card_request(bot, websocket, task, request, {"status": "ok", "data": {"message_id": 123}})
        self.assertEqual(len(websocket.sent), 1)

    async def test_numeric_and_signed_message_ids_are_valid_acknowledgements(self):
        bot = self.make_bot()
        for status, message_id in (("ok", 123), ("ok", -123), ("ok", 0), ("ok", "123"), ("async", "-123")):
            with self.subTest(status=status, message_id=message_id):
                websocket, task, request = await self.start_card_request(bot)
                await self.finish_card_request(bot, websocket, task, request, {
                    "status": status, "retcode": 0, "data": {"message_id": message_id},
                })
                self.assertEqual(len(websocket.sent), 1)
                self.assertEqual(bot._action_waiters, {})

    async def test_api_error_uses_one_text_fallback_and_cleans_waiter(self):
        bot = self.make_bot(settings={"daily_wife": {"marriage_enabled": "false"}})
        for status, retcode in (("failed", 1200), ("ok", 1200), ("failed", 0), ("unsupported", 0)):
            with self.subTest(status=status, retcode=retcode):
                websocket, task, request = await self.start_card_request(bot)
                with self.assertLogs("qq-bot", level="WARNING"):
                    await self.finish_card_request(bot, websocket, task, request, {
                        "status": status, "retcode": retcode, "data": {"message_id": 123},
                    })
                self.assert_fallback(bot, websocket)
                self.assertNotIn("/结芬", message_text(websocket.sent[-1]["params"]["message"]))

    async def test_missing_or_malformed_send_acknowledgement_falls_back(self):
        bot = self.make_bot()
        for data in (None, [], "unexpected", {}, {"forward_id": "resource-only"}, {"message_id": None},
                     {"message_id": ""}, {"message_id": True}, {"message_id": []}, {"message_id": "unknown"}):
            with self.subTest(data=data):
                websocket, task, request = await self.start_card_request(bot)
                with self.assertLogs("qq-bot", level="WARNING"):
                    await self.finish_card_request(bot, websocket, task, request, {
                        "status": "ok", "retcode": 0, "data": data,
                    })
                self.assert_fallback(bot, websocket)

    async def test_timeout_falls_back_once_without_card_retry_or_late_ack_reply(self):
        bot = self.make_bot()
        with patch("plugins.basic.HELP_CARD_TIMEOUT_SECONDS", 0.01), self.assertLogs("qq-bot", level="WARNING"):
            websocket, task, request = await self.start_card_request(bot)
            await asyncio.wait_for(task, 1)
        self.assert_fallback(bot, websocket)
        await bot._handle_raw_event(websocket, json.dumps({
            "echo": request["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 123},
        }))
        self.assert_fallback(bot, websocket)

    async def test_transport_error_cleans_waiter_and_does_not_log_sensitive_error_text(self):
        class FailingWebSocket(FakeWebSocket):
            async def send(self, payload):
                await super().send(payload)
                if json.loads(payload)["action"] == "send_group_forward_msg":
                    raise ConnectionError("fake-sensitive-token")

        bot = self.make_bot()
        websocket = FailingWebSocket()
        with self.assertLogs("qq-bot", level="WARNING") as logs:
            await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event())), 1)
        self.assert_fallback(bot, websocket)
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNotIn("fake-sensitive-token", "\n".join(logs.output))
        self.assertNotIn("fake-sensitive-token", json.dumps(websocket.sent))

    async def test_cancellation_is_not_swallowed_or_converted_to_a_fallback(self):
        bot = self.make_bot()
        websocket, task, _ = await self.start_card_request(bot)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([item["action"] for item in websocket.sent], ["send_group_forward_msg"])
        self.assertEqual(bot._action_waiters, {})

    async def test_waiting_for_card_does_not_block_other_commands(self):
        bot = self.make_bot()
        websocket, task, request = await self.start_card_request(bot)
        await asyncio.wait_for(bot._handle_raw_event(websocket, json.dumps(group_event("/hello"))), 1)
        self.assertFalse(task.done())
        self.assertEqual(message_text(websocket.sent[-1]["params"]["message"]), "啦啦啦")
        await self.finish_card_request(bot, websocket, task, request, {"status": "ok", "data": {"message_id": 123}})
        self.assertEqual([item["action"] for item in websocket.sent], ["send_group_forward_msg", "send_msg"])
        self.assertEqual(bot._action_waiters, {})

    async def test_concurrent_groups_match_out_of_order_acknowledgements(self):
        bot = self.make_bot()
        websocket, first_task, first = await self.start_card_request(bot)
        _, second_task, second = await self.start_card_request(bot, websocket, group_event(group_id=200))
        self.assertEqual(first["params"]["group_id"], "100")
        self.assertEqual(second["params"]["group_id"], "200")
        self.assertNotEqual(first["echo"], second["echo"])
        self.assertEqual(len(bot._action_waiters), 2)
        await self.finish_card_request(bot, websocket, second_task, second, {"status": "ok", "data": {"message_id": 222}})
        self.assertFalse(first_task.done())
        self.assertEqual(len(bot._action_waiters), 1)
        await self.finish_card_request(bot, websocket, first_task, first, {"status": "ok", "data": {"message_id": 111}})
        self.assertEqual([item["action"] for item in websocket.sent], ["send_group_forward_msg"] * 2)
        self.assertEqual(bot._action_waiters, {})


if __name__ == "__main__":
    unittest.main()
