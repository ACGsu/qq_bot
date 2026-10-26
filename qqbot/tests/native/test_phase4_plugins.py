import asyncio
from unittest.mock import AsyncMock

import nonebot
import pytest
import pytest_asyncio
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.drivers import Response


def group_event(*segments: MessageSegment, user_id=111, group_id=100, message_id=20):
    message = Message(segments)
    return GroupMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "normal",
        "user_id": user_id, "message_type": "group", "message_id": message_id,
        "group_id": group_id, "message": message, "original_message": message.copy(),
        "raw_message": str(message), "font": 0,
        "sender": {"user_id": user_id, "nickname": "tester", "card": "", "role": "member"},
    })


def command_event(command: str, *, user_id=111, group_id=100, target_id=None):
    segments = [MessageSegment.at("999"), MessageSegment.text(f" {command} ")]
    if target_id is not None:
        segments.append(MessageSegment.at(str(target_id)))
    return group_event(*segments, user_id=user_id, group_id=group_id)


def private_command_event(command: str, *, user_id=111, target_id=None):
    segments = [MessageSegment.at("999"), MessageSegment.text(f" {command} ")]
    if target_id is not None:
        segments.append(MessageSegment.at(str(target_id)))
    message = Message(segments)
    return PrivateMessageEvent.model_validate({
        "time": 1, "self_id": 999, "post_type": "message", "sub_type": "friend",
        "user_id": user_id, "message_type": "private", "message_id": 21,
        "message": message, "original_message": message.copy(), "raw_message": str(message),
        "font": 0, "sender": {"user_id": user_id, "nickname": "tester"},
    })


def create_bot(context):
    return context.create_bot(
        base=Bot, adapter=context.create_adapter(base=Adapter), self_id="999"
    )


@pytest_asyncio.fixture(autouse=True)
async def reset_phase4_state():
    from plugins import courtship, daily_wife, novel, song

    daily_wife.reset_state()
    courtship.reset_state()
    novel.reset_state()
    song.reset_state()
    yield
    daily_wife.reset_state()
    courtship.reset_state()
    novel.reset_state()
    song.reset_state()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_daily_wife_draw_is_group_user_scoped_and_cached(app, monkeypatch):
    from plugins import daily_wife as module
    from infrastructure.onebot import today_date_key

    monkeypatch.setattr("plugins.daily_wife.random.choice", lambda values: values[0])
    event = command_event("/今日老婆")
    members = [{"user_id": 111, "card": "self"}, {"user_id": 222, "card": "Target"}]
    record = module.DailyWifeRecord(today_date_key(), "222", "Target")
    async with app.test_matcher(module.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_api("get_group_member_list", {"group_id": 100}, result=members)
        context.should_call_send(event, module._native_message(module.format_daily_wife_new(record)), bot=bot)
        context.should_finished()

    cached = command_event("/今日群友")
    async with app.test_matcher(module.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, cached)
        context.should_pass_rule()
        context.should_call_send(cached, module._native_message(module.format_daily_wife_existing(record)), bot=bot)
        context.should_finished()
    assert module.daily_wives[("100", "111")] == record


@pytest.mark.asyncio
async def test_marriage_request_acceptance_and_timeout_cleanup(app, monkeypatch):
    from plugins import daily_wife as module
    from infrastructure.onebot import today_date_key

    module.daily_wives[("100", "111")] = module.DailyWifeRecord(today_date_key(), "222", "Target")
    request = command_event("/结芬", user_id=111)
    prompt = module._native_message([
        {"type": "at", "data": {"qq": "222"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "at", "data": {"qq": "111"}},
        {"type": "text", "data": {"text": " 想和你结芬~ 2分钟内回复：@bot /愿意 或 @bot /不愿意"}},
    ])
    async with app.test_matcher(module.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, request)
        context.should_pass_rule()
        context.should_call_send(request, prompt, bot=bot)
        context.should_finished()
    timeout_task = module.marriage_proposals_by_requester[("100", "111")].timeout_task
    assert timeout_task is not None

    response = command_event("/愿意", user_id=222)
    async with app.test_matcher(module.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, response)
        context.should_pass_rule()
        context.should_call_send(response, module.MARRIAGE_SUCCESS_MESSAGE, bot=bot)
        context.should_finished()
    assert not module.marriage_proposals_by_requester
    assert module.marriage_results[("100", "111")].result == module.MARRIAGE_RESULT_ACCEPTED
    await asyncio.sleep(0)
    assert timeout_task.cancelled()

    proposal = module.MarriageProposal("100", "333", "444", "Late", today_date_key(), 0)
    module.marriage_proposals_by_requester[("100", "333")] = proposal
    module.marriage_proposals_by_target[("100", "444")] = proposal
    timeout_bot = AsyncMock()
    monkeypatch.setattr(module, "MARRIAGE_RESPONSE_SECONDS", 0)
    await module._marriage_timeout(timeout_bot, command_event("/结芬", user_id=333), proposal)
    timeout_bot.send.assert_awaited_once()
    assert module.marriage_results[("100", "333")].result == module.MARRIAGE_RESULT_REJECTED


@pytest.mark.asyncio
async def test_daily_wife_and_courtship_reject_private_requests(app):
    from plugins import courtship, daily_wife

    wife_event = private_command_event("/今日群友")
    async with app.test_matcher(daily_wife.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, wife_event)
        context.should_pass_rule()
        context.should_call_send(wife_event, "今日群友只能在群聊中使用。", bot=bot)
        context.should_finished()

    courtship_event = private_command_event("/求偶", target_id=222)
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, courtship_event)
        context.should_pass_rule()
        context.should_call_send(courtship_event, "求偶只能在群聊中使用。", bot=bot)
        context.should_finished()


@pytest.mark.asyncio
async def test_courtship_scope_response_and_marriage_alias_priority(app, monkeypatch):
    from plugins import courtship, daily_wife

    request = command_event("/求偶", user_id=111, target_id=222)
    expected_prompt = courtship._native_message([
        {"type": "at", "data": {"qq": "222"}},
        {"type": "text", "data": {"text": " "}},
        {"type": "at", "data": {"qq": "111"}},
        {"type": "text", "data": {"text": " 向你求偶。你发情了，请问是否接受求偶？请回复：@bot /接受求偶 或 @bot /拒绝求偶"}},
    ])
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, request)
        context.should_pass_rule()
        context.should_call_send(request, expected_prompt, bot=bot)
        context.should_finished()

    wrong_group = command_event("/接受求偶", user_id=222, group_id=200)
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, wrong_group)
        context.should_pass_rule()
        context.should_call_send(wrong_group, "当前没有待回应的求偶请求。", bot=bot)
        context.should_finished()
    assert ("100", "222") in courtship.proposals_by_target

    generic = command_event("/接受", user_id=222)
    assert await daily_wife.daily_wife_command(generic)
    async with app.test_matcher(daily_wife.daily_wife) as context:
        bot = create_bot(context)
        context.receive_event(bot, generic)
        context.should_pass_rule()
        context.should_call_send(generic, "当前没有待回应的结芬请求。", bot=bot)
        context.should_finished()
    assert ("100", "222") in courtship.proposals_by_target

    monkeypatch.setattr(
        daily_wife, "get_plugin_config",
        lambda _: daily_wife.Config(daily_wife_marriage_enabled=False),
    )
    assert not await daily_wife.daily_wife_command(generic)
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, generic)
        context.should_pass_rule()
        context.should_call_send(
            generic,
            courtship._native_message([
                {"type": "at", "data": {"qq": "111"}},
                {"type": "text", "data": {"text": f" {courtship.COURTSHIP_SUCCESS_MESSAGE}"}},
            ]),
            bot=bot,
        )
        context.should_finished()
    assert not courtship.proposals_by_target


@pytest.mark.asyncio
async def test_courtship_rejects_invalid_and_conflicting_targets(app):
    from plugins import courtship

    for target_id, expected in ((111, "不能向自己求偶。"), (999, "不能向机器人求偶。")):
        event = command_event("/求偶", user_id=111, target_id=target_id)
        async with app.test_matcher(courtship.courtship) as context:
            bot = create_bot(context)
            context.receive_event(bot, event)
            context.should_pass_rule()
            context.should_call_send(event, expected, bot=bot)
            context.should_finished()

    first = command_event("/求偶", user_id=111, target_id=222)
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, first)
        context.should_pass_rule()
        context.should_call_send(first, courtship._native_message([
            {"type": "at", "data": {"qq": "222"}},
            {"type": "text", "data": {"text": " "}},
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {
                "text": " 向你求偶。你发情了，请问是否接受求偶？请回复：@bot /接受求偶 或 @bot /拒绝求偶"
            }},
        ]), bot=bot)
        context.should_finished()

    conflict = command_event("/求偶", user_id=333, target_id=222)
    async with app.test_matcher(courtship.courtship) as context:
        bot = create_bot(context)
        context.receive_event(bot, conflict)
        context.should_pass_rule()
        context.should_call_send(conflict, "对方已经有待回应的求偶请求啦，稍等一下再试。", bot=bot)
        context.should_finished()


@pytest.mark.asyncio
async def test_novel_uses_user_scope_across_groups_and_driver_service(app, monkeypatch):
    from plugins import novel as module
    from services.novel import NovelCatalog, NovelChapter, NovelInfo, NovelVolume, fetch_text

    catalog = NovelCatalog(
        NovelInfo("测试小说", "https://example/novel", "https://example/catalog"),
        [NovelVolume(1, "第一卷", "https://example/volume")],
        [NovelChapter(1, "第一章", "https://example/chapter", 1, "第一卷")],
    )
    lookup = AsyncMock(return_value=catalog)
    monkeypatch.setattr(module, "lookup_catalog", lookup)
    search = command_event("/novel 测试小说", user_id=111, group_id=100)
    async with app.test_matcher(module.novel) as context:
        bot = create_bot(context)
        context.receive_event(bot, search)
        context.should_pass_rule()
        context.should_call_send(
            search,
            "已找到：测试小说\n目录：https://example/catalog\n共 1 卷，1 章。\n接下来可输入：@bot /第1卷 或 @bot /第1章",
            bot=bot,
        )
        context.should_finished()
    lookup.assert_awaited_once()

    chapter = command_event("/第一章", user_id=111, group_id=200)
    async with app.test_matcher(module.novel) as context:
        bot = create_bot(context)
        context.receive_event(bot, chapter)
        context.should_pass_rule()
        context.should_call_send(
            chapter, "测试小说\n第1章：第一章\n所属卷：第一卷\nhttps://example/chapter", bot=bot
        )
        context.should_finished()

    other_user = command_event("/第1章", user_id=333, group_id=100)
    async with app.test_matcher(module.novel) as context:
        bot = create_bot(context)
        context.receive_event(bot, other_user)
        context.should_pass_rule()
        context.should_call_send(other_user, "请先输入：@bot /novel 小说名字", bot=bot)
        context.should_finished()

    class FakeDriver:
        def __init__(self):
            self.request_value = None

        async def request(self, request):
            self.request_value = request
            return Response(200, content="页面")

    driver = FakeDriver()
    assert await fetch_text(driver, "https://example.test", data=b"searchkey=test") == "页面"
    assert driver.request_value.method == "POST"
    assert driver.request_value.content == b"searchkey=test"


@pytest.mark.asyncio
async def test_novel_not_found_http_failure_and_reset(app, monkeypatch):
    from plugins import novel as module
    from services.novel import NovelCatalog, NovelInfo, fetch_text

    monkeypatch.setattr(module, "lookup_catalog", AsyncMock(return_value=None))
    event = command_event("/novel 不存在")
    async with app.test_matcher(module.novel) as context:
        bot = create_bot(context)
        context.receive_event(bot, event)
        context.should_pass_rule()
        context.should_call_send(event, Message("私密马赛，暂时没有该小说呢"), bot=bot)
        context.should_finished()
    assert not module.novel_sessions

    class FailedDriver:
        async def request(self, request):
            return Response(503, content="unavailable")

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await fetch_text(FailedDriver(), "https://example.test")

    module.novel_sessions["111"] = NovelCatalog(
        NovelInfo("测试", "https://example/novel", "https://example/catalog"), [], []
    )
    module.reset_state()
    assert not module.novel_sessions


@pytest.mark.asyncio
async def test_novel_search_and_catalog_fall_back_to_current_upstreams():
    from services.novel import lookup_catalog

    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>无职转生 ～到了异世界就拿出真本事～ - linovelib.com</title>
      <link>https://www.linovelib.com/novel/2013/catalog</link>
    </item></channel></rss>"""
    mobile_catalog = """
      <title>無職轉生 ～到了異世界就拿出真本事～小說線上看_嗶哩輕小說</title>
      <li class="chapter-bar chapter-li"><a href="/novel/2013/vol_1.html"><h3>第一卷</h3></a></li>
      <li class="volume-cover chapter-li"><a href="/novel/2013/vol_1.html">封面</a></li>
      <li class="chapter-li jsChapter"><a href="/novel/2013/10.html" class="chapter-li-a"><span>第一章</span></a></li>
    """

    class FakeDriver:
        def __init__(self):
            self.urls = []

        async def request(self, request):
            url = str(request.url)
            self.urls.append(url)
            if url.endswith("/S6/"):
                return Response(200, content="")
            if "bing.com/search" in url:
                return Response(200, content=rss)
            if url == "https://www.linovelib.com/novel/2013/catalog":
                return Response(403, content="blocked")
            if url == "https://tw.linovelib.com/novel/2013/catalog":
                return Response(200, content=mobile_catalog)
            raise AssertionError(url)

    driver = FakeDriver()
    catalog = await lookup_catalog(driver, "无职转生")
    assert catalog is not None
    assert catalog.novel.catalog_url == "https://tw.linovelib.com/novel/2013/catalog"
    assert [volume.url for volume in catalog.volumes] == [
        "https://tw.linovelib.com/novel/2013/vol_1.html"
    ]
    assert [chapter.url for chapter in catalog.chapters] == [
        "https://tw.linovelib.com/novel/2013/10.html"
    ]
    assert driver.urls[-2:] == [
        "https://www.linovelib.com/novel/2013/catalog",
        "https://tw.linovelib.com/novel/2013/catalog",
    ]


@pytest.mark.asyncio
async def test_novel_search_relaxes_common_slimename_alias():
    from services.novel import find_best_novel

    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>关于我转生变成史莱姆这档事小说在线阅读_伏濑作品_哔哩轻小说</title>
      <link>https://www.linovelib.com/novel/6/catalog</link>
    </item></channel></rss>"""

    class FakeDriver:
        def __init__(self):
            self.rss_calls = 0

        async def request(self, request):
            url = str(request.url)
            if url.endswith("/S6/"):
                return Response(200, content="")
            if "bing.com/search" in url:
                self.rss_calls += 1
                return Response(200, content="<rss><channel /></rss>" if self.rss_calls == 1 else rss)
            raise AssertionError(url)

    driver = FakeDriver()
    novel = await find_best_novel(driver, "关于我转生成史莱姆")
    assert novel is not None
    assert novel.catalog_url == "https://www.linovelib.com/novel/6/catalog"
    assert driver.rss_calls == 2


def test_novel_parsers_and_phase4_config_mapping():
    from infrastructure.config import RuntimeConfig
    from services.novel import NovelInfo, parse_chinese_number, parse_novel_catalog_html, parse_ordinal_command

    assert parse_ordinal_command("/第十二卷") == ("卷", 12)
    assert parse_chinese_number("二十一") == 21
    html = (
        '<div class="volume clearfix"><h2><a href="/vol_1.html">第一卷</a></h2>'
        '<li><a href="/chapter_1.html">第一章</a></li></div>'
    )
    catalog = parse_novel_catalog_html(
        NovelInfo("测试", "https://example/novel", "https://example/catalog"), html
    )
    assert [volume.title for volume in catalog.volumes] == ["第一卷"]
    assert [chapter.title for chapter in catalog.chapters] == ["第一章"]

    false_options = RuntimeConfig().nonebot_options(
        {"plugin_settings": {"daily_wife": {"marriage_enabled": False}}}, {}
    )
    true_options = RuntimeConfig().nonebot_options(
        {"plugin_settings": {"daily_wife": {"marriage_enabled": "true"}}}, {}
    )
    assert false_options["daily_wife_marriage_enabled"] is False
    assert true_options["daily_wife_marriage_enabled"] is True

