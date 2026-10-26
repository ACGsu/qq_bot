import nonebot
import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed


def group_event(*segments: MessageSegment, user_id=111, message_id=10):
    message = Message(segments or (MessageSegment.text("普通消息"),))
    return GroupMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "normal",
        "user_id": user_id, "message_type": "group", "message_id": message_id,
        "group_id": 100, "message": message, "original_message": message.copy(),
        "raw_message": str(message), "font": 0,
        "sender": {"user_id": user_id, "nickname": "tester", "card": "", "role": "member"},
    })


def private_event(*segments: MessageSegment, user_id=111):
    message = Message(segments)
    return PrivateMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "friend",
        "user_id": user_id, "message_type": "private", "message_id": 11,
        "message": message, "original_message": message.copy(), "raw_message": str(message),
        "font": 0, "sender": {"user_id": user_id, "nickname": "tester"},
    })


def create_bot(context):
    adapter = context.create_adapter(base=Adapter)
    return context.create_bot(base=Bot, adapter=adapter, self_id="999")


@pytest.mark.asyncio
async def test_basic_hello_requires_original_real_mention(app):
    from plugins.basic import basic

    event = group_event(MessageSegment.at("999"), MessageSegment.text(" /hello"))
    async with app.test_matcher(basic) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(event, "啦啦啦", bot=bot)
        context.should_finished()

    fake = group_event(MessageSegment.text("@bot /hello"))
    async with app.test_matcher(basic) as context:
        context.receive_event(create_bot(context), fake)
        context.should_not_pass_rule()


@pytest.mark.asyncio
async def test_basic_help_adapter_data_failure_falls_back_once(app):
    from plugins.basic import HELP_CARD_FALLBACK_PREFIX, basic
    from services.help import build_help_card, format_help

    settings = {
        "rps": {"ban_seconds": "300"},
        "auto_emoji": {"target_qq": "111", "emoji_ids": "12951,127852"},
    }
    enabled = ("auto_emoji", "rps", "basic")
    event = group_event(MessageSegment.at("999"), MessageSegment.text(" /help"))
    data = build_help_card("999", enabled, settings)
    data["group_id"] = 100
    async with app.test_matcher(basic) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_api("send_group_forward_msg", data, result={})
        context.should_call_send(event, HELP_CARD_FALLBACK_PREFIX + format_help(enabled, settings), bot=bot)
        context.should_finished()

    async with app.test_matcher(basic) as context:
        context.receive_event(create_bot(context), event)
        context.should_pass_rule()
        context.should_call_api("send_group_forward_msg", data, result={"message_id": 123})


@pytest.mark.asyncio
async def test_rps_extracts_target_at_and_uses_typed_duration(app, monkeypatch):
    from plugins.rps import RpsResult, _native_message, format_rps_result, rps

    choices = iter(("石头", "剪刀"))
    monkeypatch.setattr("plugins.rps.random.choice", lambda _: next(choices))
    result = RpsResult("石头", "剪刀", "222", "111")
    event = group_event(MessageSegment.at("999"), MessageSegment.text(" /猜拳 "), MessageSegment.at("222"))
    async with app.test_matcher(rps) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_api("set_group_ban", {"group_id": 100, "user_id": 222, "duration": 300}, result=None)
        context.should_call_send(event, _native_message(format_rps_result("111", "222", result, 300)), bot=bot)
        context.should_finished()


@pytest.mark.asyncio
async def test_rps_private_restriction(app):
    from plugins.rps import rps

    event = private_event(MessageSegment.at("999"), MessageSegment.text(" /猜拳 "), MessageSegment.at("222"))
    async with app.test_matcher(rps) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(event, "猜拳只能在群聊中使用。", bot=bot)
        context.should_finished()


@pytest.mark.asyncio
async def test_rps_reports_owner_ban_failure_instead_of_crashing(app, monkeypatch):
    from plugins.rps import RpsResult, _native_message, format_rps_result, rps

    choices = iter(("石头", "剪刀"))
    monkeypatch.setattr("plugins.rps.random.choice", lambda _: next(choices))
    result = RpsResult("石头", "剪刀", "222", "111")
    failure = ActionFailed(
        status="failed",
        retcode=1200,
        data=None,
        message="cannot ban owner",
        wording="cannot ban owner",
        echo="1",
        stream="normal-action",
    )
    event = group_event(MessageSegment.at("999"), MessageSegment.text(" /猜拳 "), MessageSegment.at("222"))
    async with app.test_matcher(rps) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_api(
            "set_group_ban",
            {"group_id": 100, "user_id": 222, "duration": 300},
            exception=failure,
        )
        context.should_call_send(
            event,
            _native_message(format_rps_result(
                "111",
                "222",
                result,
                300,
                "群主无法被禁言，本局不执行禁言。",
            )),
            bot=bot,
        )
        context.should_finished()


@pytest.mark.asyncio
async def test_rps_rejects_bot_target(app):
    from plugins.rps import rps

    event = group_event(MessageSegment.at("999"), MessageSegment.text(" /猜拳 "), MessageSegment.at("999"))
    async with app.test_matcher(rps) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(event, "不能和机器人猜拳。", bot=bot)
        context.should_finished()


@pytest.mark.asyncio
async def test_auto_emoji_is_passive_and_filters_target(app):
    from plugins.auto_emoji import auto_emoji

    event = group_event(MessageSegment.text("无需 @bot"), user_id=111, message_id=222333)
    async with app.test_matcher(auto_emoji) as context:
        context.receive_event(create_bot(context), event)
        context.should_pass_rule()
        context.should_call_api("set_msg_emoji_like", {"message_id": 222333, "emoji_id": 12951}, result=None)
        context.should_call_api("set_msg_emoji_like", {"message_id": 222333, "emoji_id": 127852}, result=None)

    ignored = group_event(MessageSegment.text("普通消息"), user_id=222, message_id=5)
    async with app.test_matcher(auto_emoji) as context:
        context.receive_event(create_bot(context), ignored)
        context.should_not_pass_rule()


def test_phase3_business_helpers_and_config_priority():
    from infrastructure.config import RuntimeConfig
    from plugins.auto_emoji import parse_emoji_ids
    from plugins.basic import _has_message_id, dispatch_command
    from plugins.rps import RpsResult, format_rps_result

    assert dispatch_command("/hello") == "啦啦啦"
    assert "/猜拳" in dispatch_command("/help", ("rps", "basic"), {})
    assert "/课表" not in dispatch_command("/help", ("rps", "basic"), {})
    assert _has_message_id({"message_id": "-12"})
    assert not _has_message_id({"message_id": True})
    assert parse_emoji_ids("12951，127852;12951 custom") == [12951, 127852, "custom"]
    message = format_rps_result("111", "222", RpsResult("石头", "剪刀", "222", "111"), 61)
    assert "禁言61秒" in "".join(segment["data"].get("text", "") for segment in message)

    options = RuntimeConfig().nonebot_options(
        {"plugin_settings": {
            "rps": {"ban_seconds": "300"},
            "auto_emoji": {"target_qq": "", "emoji_id": "777"},
        }},
        {"AUTO_CANDY_QQ": "should-not-win", "AUTO_EMOJI_IDS": "888"},
    )
    assert options["rps_ban_seconds"] == 300
    assert options["auto_emoji_target_qq"] == ""
    assert options["auto_emoji_ids"] == "777"

