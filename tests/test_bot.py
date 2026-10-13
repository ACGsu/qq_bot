import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugin_control import (
    PluginConfig,
    default_enabled_plugin_ids,
    load_enabled_plugin_ids,
    load_plugin_config,
    save_enabled_plugin_ids,
    save_plugin_config,
)
from bot import (
    BAN_SECONDS,
    BasicCommandPlugin,
    BotConfig,
    CommandContext,
    DailyWifePlugin,
    DailyWifeRecord,
    NapCatBot,
    command_argument,
    dispatch_command,
    extract_command_after_mention,
    extract_command_context_after_mention,
    format_daily_wife_existing,
    format_daily_wife_new,
    format_rps_result,
    member_display_name,
    NovelInfo,
    parse_chinese_number,
    parse_novel_catalog,
    parse_novel_links,
    parse_ordinal_command,
    parse_song_search_response,
    parse_song_selection,
    parse_song_size_mb,
    play_rock_paper_scissors,
    wife_candidates,
    detect_song_extension,
    fetch_song_cover,
    fetch_song_download,
    fetch_song_url,
    format_song_details,
    format_song_search_results,
    GroupSummaryPlugin,
    join_song_artists,
    safe_song_filename,
    song_quality_label,
    message_to_text,
    summarize_group_messages,
    extract_history_messages,
    SongApiError,
    SongDownload,
    SongResult,
    SongUnavailableError,
    today_date_key,
)
from plugins.daily_wife import MARRIAGE_FAIL_MESSAGE, MARRIAGE_SUCCESS_MESSAGE
from plugins.courtship import CourtshipPlugin, COURTSHIP_REJECT_MESSAGE, COURTSHIP_SUCCESS_MESSAGE
from plugins.summary import (
    DeepSeekApiClient,
    DeepSeekConfigurationError,
    extract_deepseek_api_reply,
    fetch_group_history,
    parse_summary_limit,
)
from plugins.auto_emoji import parse_emoji_ids


class PluginControlTest(unittest.TestCase):
    def test_missing_plugin_config_enables_all_plugins(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.json"

            self.assertEqual(load_enabled_plugin_ids(config_path), default_enabled_plugin_ids())

    def test_plugin_config_ignores_unknown_plugin_ids(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "plugins.json"
            config_path.write_text('{"enabled_plugins": ["song", "unknown", "basic"]}', encoding="utf-8")

            self.assertEqual(load_enabled_plugin_ids(config_path), ("song", "basic"))

    def test_save_plugin_config_round_trips_enabled_plugins(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "plugins.json"

            save_enabled_plugin_ids(("novel", "rps"), config_path)

            self.assertEqual(load_enabled_plugin_ids(config_path), ("rps", "novel"))

    def test_save_plugin_config_round_trips_plugin_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "plugins.json"

            save_plugin_config(
                ("auto_emoji", "basic"),
                {"auto_emoji": {"target_qq": "111", "emoji_id": "127852"}},
                config_path,
            )

            plugin_config = load_plugin_config(config_path)
            self.assertEqual(plugin_config.enabled_plugin_ids, ("auto_emoji", "basic"))
            self.assertEqual(plugin_config.plugin_settings["auto_emoji"]["target_qq"], "111")


class BotCommandTest(unittest.TestCase):
    def test_segment_message_requires_bot_mention(self) -> None:
        message = [
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": " /hello"}},
        ]

        self.assertEqual(extract_command_after_mention(message, "123"), "/hello")
        self.assertIsNone(extract_command_after_mention(message, "456"))

    def test_cq_message_requires_bot_mention(self) -> None:
        self.assertEqual(extract_command_after_mention("[CQ:at,qq=123] /help", "123"), "/help")
        self.assertIsNone(extract_command_after_mention("[CQ:at,qq=456] /help", "123"))

    def test_dispatch_command(self) -> None:
        self.assertIn("/hello", dispatch_command("/help"))
        self.assertIn("/猜拳", dispatch_command("/help"))
        self.assertIn("/结芬", dispatch_command("/help"))
        self.assertIn("/求偶", dispatch_command("/help"))
        self.assertIn("/song", dispatch_command("/help"))
        self.assertIn("/总结", dispatch_command("/help"))
        self.assertEqual(dispatch_command("/hello"), "啦啦啦")
        self.assertIsNone(dispatch_command("/unknown"))

    def test_dispatch_help_hides_disabled_plugin_commands(self) -> None:
        help_text = dispatch_command("/help", ("daily_wife", "song", "novel", "basic"))

        self.assertIn("/hello", help_text)
        self.assertIn("/今日群友", help_text)
        self.assertIn("/song", help_text)
        self.assertNotIn("/猜拳", help_text)
        self.assertNotIn("/求偶", help_text)

    def test_command_argument(self) -> None:
        self.assertEqual(command_argument("/song 你好"), "你好")
        self.assertEqual(command_argument("/song    你好 世界"), "你好 世界")
        self.assertEqual(command_argument("/song"), "")

    def test_segment_rps_target_mention_is_preserved(self) -> None:
        message = [
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": " /猜拳 "}},
            {"type": "at", "data": {"qq": "456"}},
        ]

        context = extract_command_context_after_mention(message, "123")

        self.assertIsNotNone(context)
        self.assertEqual(context.text, "/猜拳")
        self.assertEqual(context.mentions, ["456"])

    def test_cq_rps_target_mention_is_preserved(self) -> None:
        context = extract_command_context_after_mention("[CQ:at,qq=123] /猜拳 [CQ:at,qq=456]", "123")

        self.assertIsNotNone(context)
        self.assertEqual(context.text, "/猜拳")
        self.assertEqual(context.mentions, ["456"])

    def test_rps_loser_is_target_when_challenger_wins(self) -> None:
        with patch("plugins.rps.random.choice", side_effect=["石头", "剪刀"]):
            result = play_rock_paper_scissors("111", "222")
        message = format_rps_result("111", "222", result)
        rendered_text = "".join(segment["data"]["text"] for segment in message if segment["type"] == "text")
        mentions = [segment["data"]["qq"] for segment in message if segment["type"] == "at"]

        self.assertEqual(result.winner_id, "111")
        self.assertEqual(result.loser_id, "222")
        self.assertIn("禁言1分钟", rendered_text)
        self.assertNotIn("111", rendered_text)
        self.assertNotIn("222", rendered_text)
        self.assertEqual(mentions, ["111", "222", "222"])
        self.assertEqual(BAN_SECONDS, 60)

    def test_daily_wife_candidates_exclude_requester(self) -> None:
        members = [
            {"user_id": 111, "card": "requester"},
            {"user_id": 222, "card": "target"},
            {"user_id": "", "card": "invalid"},
        ]

        self.assertEqual(wife_candidates(members, "111"), [{"user_id": 222, "card": "target"}])

    def test_daily_wife_member_display_name_prefers_group_card(self) -> None:
        self.assertEqual(member_display_name({"card": "Card", "nickname": "Nick"}), "Card")
        self.assertEqual(member_display_name({"card": "", "nickname": "Nick"}), "Nick")
        self.assertEqual(member_display_name({}), "\u8fd9\u4f4d\u7fa4\u53cb")

    def test_daily_wife_new_message_sends_avatar_and_mentions_target(self) -> None:
        record = DailyWifeRecord("2026-06-23", "222", "Nick")

        message = format_daily_wife_new(record)
        rendered_text = "".join(segment["data"].get("text", "") for segment in message if segment["type"] == "text")

        self.assertEqual(message[0]["type"], "image")
        self.assertIn("nk=222", message[0]["data"]["file"])
        self.assertEqual([segment["data"]["qq"] for segment in message if segment["type"] == "at"], ["222"])
        self.assertIn("Nick\u662f\u4f60\u7684\u4eca\u65e5\u7fa4\u53cb\u8001\u5a46~", rendered_text)
        self.assertIn("结芬~", rendered_text)

    def test_daily_wife_existing_message_mentions_cached_target(self) -> None:
        record = DailyWifeRecord("2026-06-23", "222", "Nick")

        message = format_daily_wife_existing(record)
        rendered_text = "".join(segment["data"].get("text", "") for segment in message if segment["type"] == "text")

        self.assertEqual([segment["data"]["qq"] for segment in message if segment["type"] == "at"], ["222"])
        self.assertIn("\u4f60\u5df2\u7ecf\u62bd\u53d6\u4e86\u4eca\u65e5\u7684\u7fa4\u53cb\u8001\u5a46", rendered_text)
        self.assertIn("Nick", rendered_text)
        self.assertIn("结芬~", rendered_text)

    def test_parse_ordinal_command(self) -> None:
        self.assertEqual(parse_ordinal_command("/第3章"), ("章", 3))
        self.assertEqual(parse_ordinal_command("/第十二卷"), ("卷", 12))
        self.assertEqual(parse_chinese_number("二十一"), 21)
        self.assertIsNone(parse_ordinal_command("/第0章"))

    def test_parse_novel_links(self) -> None:
        html = '<a href="/novel/123.html">测试小说</a><a href="/novel/123.html">立即阅读</a>'
        novels = parse_novel_links(html)

        self.assertEqual(len(novels), 1)
        self.assertEqual(novels[0].title, "测试小说")
        self.assertEqual(novels[0].catalog_url, "https://www.linovelib.com/novel/123/catalog")

    def test_parse_novel_catalog(self) -> None:
        html = """
        <div class="volume clearfix">
          <h2 class="v-line"><a href="/novel/123/vol_1.html">第一卷</a></h2>
          <ul class="chapter-list clearfix">
            <li><a href="/novel/123/10.html">第一章</a></li>
            <li><a href="/novel/123/11.html">第二章</a></li>
          </ul>
        </div>
        <div class="volume clearfix">
          <h2 class="v-line"><a href="/novel/123/vol_2.html">第二卷</a></h2>
          <ul class="chapter-list clearfix">
            <li><a href="/novel/123/12.html">第三章</a></li>
          </ul>
        </div>
        """
        novel = NovelInfo("测试小说", "https://www.linovelib.com/novel/123.html", "https://www.linovelib.com/novel/123/catalog")

        with patch("plugins.novel.fetch_text", return_value=html):
            catalog = parse_novel_catalog(novel)

        self.assertEqual([volume.title for volume in catalog.volumes], ["第一卷", "第二卷"])
        self.assertEqual(catalog.chapters[2].title, "第三章")
        self.assertEqual(catalog.chapters[2].volume_title, "第二卷")

    def test_parse_song_search_response(self) -> None:
        payload = [
            {
                "id": "33911781",
                "name": "secret base ~君がくれたもの~ (10 years after Ver.)",
                "artist": ["茅野愛衣", "戸松遥", "早見沙織"],
                "album": "secret base ～君がくれたもの～",
                "pic_id": "109951166198069948",
                "source": "netease",
            }
        ]

        songs = parse_song_search_response(payload)

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].title, "secret base ~君がくれたもの~ (10 years after Ver.)")
        self.assertEqual(songs[0].artist, "茅野愛衣、戸松遥、早見沙織")
        self.assertEqual(songs[0].album, "secret base ～君がくれたもの～")
        self.assertEqual(songs[0].song_id, "33911781")
        self.assertEqual(songs[0].pic_id, "109951166198069948")
        self.assertEqual(songs[0].source, "netease")

    def test_parse_song_search_response_skips_invalid_items(self) -> None:
        payload = [
            {"id": "1", "name": "歌"},
            {"id": "2"},
            {"name": "无 ID"},
            "not-a-dict",
        ]

        songs = parse_song_search_response(payload)

        self.assertEqual([song.song_id for song in songs], ["1"])
        self.assertEqual(songs[0].artist, "未知歌手")
        self.assertEqual(songs[0].album, "")

    def test_join_song_artists_accepts_list_and_string(self) -> None:
        self.assertEqual(join_song_artists(["A", "B"]), "A、B")
        self.assertEqual(join_song_artists("A / B"), "A / B")
        self.assertEqual(join_song_artists(None), "未知歌手")
        self.assertEqual(join_song_artists([]), "未知歌手")

    def test_parse_song_size_mb_handles_bytes_and_kilobytes(self) -> None:
        self.assertEqual(parse_song_size_mb(14211701), 13.6)
        self.assertEqual(parse_song_size_mb(14211), 13.9)
        self.assertIsNone(parse_song_size_mb(0))
        self.assertIsNone(parse_song_size_mb(None))
        self.assertIsNone(parse_song_size_mb("bad"))

    def test_fetch_song_url_parses_direct_link(self) -> None:
        with patch(
            "plugins.song.fetch_json",
            return_value={"url": "https://m701.music.126.net/example.mp3", "br": 320, "size": 14211701},
        ) as fetch:
            download = fetch_song_url("33911781")

        self.assertEqual(download, SongDownload("https://m701.music.126.net/example.mp3", 13.6, 320))
        self.assertIn("types=url", fetch.call_args.args[0])
        self.assertIn("br=320", fetch.call_args.args[0])

    def test_fetch_song_url_rejects_missing_link(self) -> None:
        with patch("plugins.song.fetch_json", return_value={"url": "", "br": -1, "size": 0}):
            with self.assertRaises(SongUnavailableError):
                fetch_song_url("notexist123")

    def test_fetch_song_download_accepts_requested_quality(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "pic-1", "netease")
        with patch(
            "plugins.song.fetch_song_url",
            return_value=SongDownload("https://cdn.example.com/song.mp3", 13.6, 320),
        ) as fetch:
            download = fetch_song_download(song, "320")

        self.assertEqual(download.bitrate, 320)
        self.assertEqual(fetch.call_args.kwargs["bitrate"], "320")
        self.assertEqual(fetch.call_args.kwargs["source"], "netease")

    def test_fetch_song_download_accepts_lossless_with_higher_returned_bitrate(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "", "netease")
        with patch(
            "plugins.song.fetch_song_url",
            return_value=SongDownload("https://cdn.example.com/song.flac", 71.2, 813),
        ):
            download = fetch_song_download(song, "999")

        self.assertEqual(download.bitrate, 813)

    def test_fetch_song_download_rejects_quality_below_floor(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "", "netease")
        with patch(
            "plugins.song.fetch_song_url",
            return_value=SongDownload("https://cdn.example.com/song.mp3", 5.7, 128),
        ):
            with self.assertRaises(SongApiError) as ctx:
                fetch_song_download(song, "320")

        self.assertIn("当前音源不支持所选音质（320kbps，实际仅支持 128kbps），请降低音质后重试。", str(ctx.exception))

    def test_fetch_song_download_prompts_lower_quality_when_unsupported(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "", "netease")
        responses = [SongUnavailableError("no url"), SongDownload("https://cdn.example.com/song.mp3", 13.6, 320)]
        with patch("plugins.song.fetch_song_url", side_effect=responses) as fetch:
            with self.assertRaises(SongApiError) as ctx:
                fetch_song_download(song, "999")

        self.assertIn("当前音源不支持所选音质（999kbps），请降低音质后重试。", str(ctx.exception))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.kwargs["bitrate"], "320")

    def test_fetch_song_download_reports_unavailable_when_probe_also_fails(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "", "netease")
        with patch("plugins.song.fetch_song_url", side_effect=SongUnavailableError("no url")):
            with self.assertRaises(SongApiError) as ctx:
                fetch_song_download(song, "999")

        self.assertIn("该歌曲暂时无法获取下载链接", str(ctx.exception))

    def test_fetch_song_download_320_failure_raises_unavailable_without_probe(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "", "netease")
        with patch("plugins.song.fetch_song_url", side_effect=SongUnavailableError("no url")) as fetch:
            with self.assertRaises(SongUnavailableError):
                fetch_song_download(song, "320")

        self.assertEqual(fetch.call_count, 1)

    def test_fetch_song_cover_downloads_image_bytes(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "image/jpeg"}

            def read(self, size: int = -1) -> bytes:
                return b"cover-bytes"

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        with patch(
            "plugins.song.fetch_json",
            return_value={"url": "https://p2.music.126.net/cover.jpg"},
        ), patch("plugins.song.request.urlopen", return_value=FakeResponse()):
            cover = fetch_song_cover("109951166198069948")

        self.assertEqual(cover, b"cover-bytes")

    def test_fetch_song_cover_returns_none_without_pic_id(self) -> None:
        self.assertIsNone(fetch_song_cover(""))

    def test_fetch_song_cover_returns_none_on_failure(self) -> None:
        with patch("plugins.song.fetch_json", side_effect=RuntimeError("boom")):
            self.assertIsNone(fetch_song_cover("pic-1"))

    def test_song_selection_and_result_format(self) -> None:
        songs = [SongResult("song-1", "歌", "歌手", "专辑", "")]

        self.assertEqual(parse_song_selection("2"), (2, None))
        self.assertEqual(parse_song_selection("选择 3"), (3, None))
        self.assertEqual(parse_song_selection("2+320"), (2, "320"))
        self.assertEqual(parse_song_selection("选择 2+999"), (2, "999"))
        self.assertEqual(parse_song_selection("2 740"), (2, "740"))
        self.assertIsNone(parse_song_selection("歌名"))

        rendered = format_song_search_results(songs)
        self.assertIn("1. 歌 - 歌手，专辑：专辑", rendered)
        self.assertIn("@bot /song 2", rendered)
        self.assertIn("2+320", rendered)

    def test_format_song_details_includes_quality_and_size(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "pic-1")
        rendered = format_song_details(song, SongDownload("https://cdn.example.com/song.mp3", 13.6, 320))

        self.assertIn("歌曲：歌", rendered)
        self.assertIn("歌手：歌手", rendered)
        self.assertIn("专辑：专辑", rendered)
        self.assertIn("音质：320kbps（约 13.6 MB）", rendered)

    def test_format_song_details_marks_lossless(self) -> None:
        song = SongResult("song-1", "歌", "歌手", "专辑", "")
        rendered = format_song_details(song, SongDownload("https://cdn.example.com/song.flac", 71.2, 1682))

        self.assertIn("音质：1682kbps（无损）（约 71.2 MB）", rendered)

    def test_song_quality_label(self) -> None:
        self.assertEqual(song_quality_label(320), "320kbps")
        self.assertEqual(song_quality_label(740), "740kbps（无损）")
        self.assertEqual(song_quality_label(0), "未知音质")

    def test_safe_song_filename(self) -> None:
        self.assertEqual(safe_song_filename('A/B: C?'), "A_B_ C.mp3")
        self.assertEqual(safe_song_filename("歌", ".flac"), "歌.flac")
        self.assertEqual(safe_song_filename("歌", "flac"), "歌.flac")

    def test_detect_song_extension_by_magic_bytes(self) -> None:
        cases = {
            "flac": (b"fLaC" + b"\x00" * 8, ".flac"),
            "mp3_id3": (b"ID3\x03\x00\x00\x00" + b"\x00" * 6, ".mp3"),
            "mp3_frame": (b"\xff\xfb\x90\x00" + b"\x00" * 8, ".mp3"),
            "ogg": (b"OggS\x00\x02" + b"\x00" * 6, ".ogg"),
            "wav": (b"RIFF\x24\x00\x00\x00WAVE", ".wav"),
            "m4a": (b"\x00\x00\x00\x18ftypM4A ", ".m4a"),
            "unknown": (b"\x00\x01\x02\x03" + b"\x00" * 8, ".mp3"),
        }
        with TemporaryDirectory() as temp:
            base = Path(temp)
            for name, (data, expected) in cases.items():
                path = base / f"{name}.bin"
                path.write_bytes(data)
                self.assertEqual(detect_song_extension(path), expected, name)
            fallback = base / "fallback.bin"
            fallback.write_bytes(cases["unknown"][0])
            self.assertEqual(detect_song_extension(fallback, "https://cdn.example.com/a/b.flac"), ".flac")


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def bot_config(**overrides):
    values = {
        "websocket_mode": "server",
        "ws_url": "ws://example.invalid",
        "access_token": None,
        "bot_qq": "999",
        "listen_host": "127.0.0.1",
        "listen_port": 8080,
        "reconnect_seconds": 5.0,
    }
    values.update(overrides)
    return BotConfig(**values)


def auto_emoji_plugin_config(
    target_qq: str = "111",
    emoji_ids: str | None = "127852,12951",
    emoji_id: str | None = None,
) -> PluginConfig:
    settings = {"target_qq": target_qq}
    if emoji_ids is not None:
        settings["emoji_ids"] = emoji_ids
    if emoji_id is not None:
        settings["emoji_id"] = emoji_id
    return PluginConfig(
        ("auto_emoji",),
        {"auto_emoji": settings},
    )


class AutoEmojiLikeTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_emoji_ids_accepts_multiple_separators_and_deduplicates(self) -> None:
        self.assertEqual(parse_emoji_ids("127852,12951  12951;custom"), [127852, 12951, "custom"])

    async def test_group_message_from_target_gets_configured_reactions(self) -> None:
        with patch("bot.load_plugin_config", return_value=auto_emoji_plugin_config("111")):
            bot = NapCatBot(bot_config())
        websocket = FakeWebSocket()

        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 111,
                    "message_id": 222333,
                    "self_id": 999,
                    "message": "hello",
                }
            ),
        )

        self.assertEqual(len(websocket.sent), 2)
        self.assertEqual([payload["action"] for payload in websocket.sent], ["set_msg_emoji_like", "set_msg_emoji_like"])
        self.assertEqual(
            [payload["params"] for payload in websocket.sent],
            [
                {"message_id": 222333, "emoji_id": 127852},
                {"message_id": 222333, "emoji_id": 12951},
            ],
        )

    async def test_auto_emoji_keeps_legacy_single_emoji_id_setting(self) -> None:
        with patch("bot.load_plugin_config", return_value=auto_emoji_plugin_config("111", emoji_ids=None, emoji_id="127852")):
            bot = NapCatBot(bot_config())
        websocket = FakeWebSocket()

        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 111,
                    "message_id": 222333,
                    "self_id": 999,
                    "message": "hello",
                }
            ),
        )

        self.assertEqual(len(websocket.sent), 1)
        self.assertEqual(websocket.sent[0]["params"], {"message_id": 222333, "emoji_id": 127852})

    async def test_auto_emoji_ignores_non_target_and_private_messages(self) -> None:
        with patch("bot.load_plugin_config", return_value=auto_emoji_plugin_config("111")):
            bot = NapCatBot(bot_config())
        websocket = FakeWebSocket()

        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 222,
                    "message_id": 1,
                    "message": "hello",
                }
            ),
        )
        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 111,
                    "message_id": 2,
                    "message": "hello",
                }
            ),
        )

        self.assertEqual(websocket.sent, [])

    async def test_auto_emoji_requires_message_id(self) -> None:
        with patch("bot.load_plugin_config", return_value=auto_emoji_plugin_config("111")):
            bot = NapCatBot(bot_config())
        websocket = FakeWebSocket()

        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 111,
                    "message": "hello",
                }
            ),
        )

        self.assertEqual(websocket.sent, [])

    async def test_auto_emoji_plugin_can_be_disabled(self) -> None:
        with patch("bot.load_plugin_config", return_value=PluginConfig(("basic",), {})):
            bot = NapCatBot(bot_config())
        websocket = FakeWebSocket()

        await bot._handle_raw_event(
            websocket,
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 111,
                    "message_id": 222333,
                    "message": "hello",
                }
            ),
        )

        self.assertEqual(websocket.sent, [])


class FakeSummaryBot:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.replies = []

    async def _send_action_request(self, websocket, action, params, timeout=10):
        self.requests.append((action, params, timeout))
        if self.responses:
            return self.responses.pop(0)
        return {"data": {"messages": []}}

    async def _send_reply(self, websocket, event, message):
        self.replies.append(message)


class FakeDeepSeekClient:
    def __init__(self, summary: str = "DeepSeek 总结结果") -> None:
        self.summary = summary
        self.calls = []

    def summarize_group_chat(self, transcript: str, requested_count: int) -> str:
        self.calls.append((transcript, requested_count))
        return self.summary


class GroupSummaryTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_summary_limit_accepts_count_argument(self) -> None:
        self.assertEqual(parse_summary_limit("/总结"), 100)
        self.assertEqual(parse_summary_limit("/总结 20"), 20)
        self.assertEqual(parse_summary_limit("/总结 条数20"), 20)
        self.assertEqual(parse_summary_limit("/总结 条数 20"), 20)
        self.assertEqual(parse_summary_limit("/总结 800"), 500)
        self.assertIsNone(parse_summary_limit("/总结 最近二十条"))

    def test_deepseek_api_reply_extracts_chat_completion_content(self) -> None:
        body = json.dumps({"choices": [{"message": {"content": "总结结果"}}]}, ensure_ascii=False)

        self.assertEqual(extract_deepseek_api_reply(body), "总结结果")

    def test_deepseek_api_client_requires_api_key(self) -> None:
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(DeepSeekConfigurationError):
                DeepSeekApiClient.from_settings({})

    def test_extract_history_messages_accepts_common_shapes(self) -> None:
        messages = [{"message_id": 1}, {"message_id": 2}]

        self.assertEqual(extract_history_messages({"data": messages}), messages)
        self.assertEqual(extract_history_messages({"data": {"messages": messages}}), messages)
        self.assertEqual(extract_history_messages({"data": {"message": messages}}), messages)

    def test_message_to_text_handles_segments(self) -> None:
        message = {
            "message": [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "at", "data": {"qq": "123"}},
                {"type": "image", "data": {"file": "x.jpg"}},
            ]
        }

        self.assertEqual(message_to_text(message), "看这个 @123 [图片]")

    def test_summarize_group_messages_mentions_topics_and_speakers(self) -> None:
        messages = [
            {"message_id": 1, "time": 1, "sender": {"nickname": "Alice"}, "raw_message": "今晚一起打游戏吗"},
            {"message_id": 2, "time": 2, "sender": {"nickname": "Bob"}, "raw_message": "游戏可以，顺便讨论服务器配置"},
            {"message_id": 3, "time": 3, "sender": {"nickname": "Alice"}, "raw_message": "服务器配置我看一下"},
        ]

        summary = summarize_group_messages(messages)

        self.assertIn("群聊上下文总结", summary)
        self.assertIn("Alice 2条", summary)
        self.assertIn("服务器配置", summary)
        self.assertIn("代表性上文", summary)

    async def test_fetch_group_history_uses_existing_oldest_sequence_as_cursor(self) -> None:
        fake_bot = FakeSummaryBot(
            (
                {
                    "data": {
                        "messages": [
                            {"message_id": 200, "message_seq": 200, "raw_message": "第一批较新"},
                            {"message_id": 198, "message_seq": 198, "raw_message": "第一批较旧"},
                        ]
                    }
                },
                {
                    "data": {
                        "messages": [
                            {"message_id": 198, "message_seq": 198, "raw_message": "重叠消息"},
                            {"message_id": 150, "message_seq": 150, "raw_message": "第二批更旧"},
                        ]
                    }
                },
            )
        )

        messages = await fetch_group_history(fake_bot, None, "100", 3)

        self.assertEqual([message["message_id"] for message in messages], [200, 198, 150])
        self.assertNotIn("message_seq", fake_bot.requests[0][1])
        self.assertEqual(fake_bot.requests[1][1]["message_seq"], 198)

    async def test_summary_plugin_fetches_history_and_replies(self) -> None:
        deepseek_client = FakeDeepSeekClient()
        plugin = GroupSummaryPlugin(deepseek_client=deepseek_client)
        messages = [
            {"message_id": 20, "message_seq": 20, "time": 1, "sender": {"nickname": "Alice"}, "raw_message": "大家在聊项目发布"},
            {"message_id": 19, "message_seq": 19, "time": 2, "sender": {"nickname": "Bob"}, "raw_message": "项目发布前要检查配置"},
        ]
        fake_bot = FakeSummaryBot(({"data": {"messages": messages}}, {"data": {"messages": []}}))

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/总结 条数2", []),
        )

        self.assertEqual(fake_bot.requests[0][0], "get_group_msg_history")
        self.assertEqual(fake_bot.requests[0][1]["group_id"], 100)
        self.assertEqual(fake_bot.requests[0][1]["count"], 2)
        self.assertEqual(len(fake_bot.replies), 2)
        self.assertIn("最近 2 条", fake_bot.replies[0])
        self.assertEqual(fake_bot.replies[1], "DeepSeek 总结结果")
        self.assertEqual(deepseek_client.calls[0][1], 2)
        self.assertIn("项目发布", deepseek_client.calls[0][0])

    async def test_summary_plugin_requires_group_chat(self) -> None:
        plugin = GroupSummaryPlugin()
        fake_bot = FakeSummaryBot(())

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "private", "user_id": 111},
            CommandContext("/总结", []),
        )

        self.assertEqual(fake_bot.replies[-1], "总结只能在群聊中使用。")


class FakeReplyBot:
    def __init__(self) -> None:
        self.replies = []
        self.logger = self
        self.config = type("Config", (), {"bot_qq": "999"})()

    async def _send_reply(self, websocket, event, message):
        self.replies.append(message)

    def exception(self, *_):
        raise AssertionError("unexpected bot logger exception")


class DailyWifeMarriageTest(unittest.IsolatedAsyncioTestCase):
    async def test_marriage_request_can_be_accepted(self) -> None:
        plugin = DailyWifePlugin()
        fake_bot = FakeReplyBot()
        plugin.daily_wives[("100", "111")] = DailyWifeRecord(today_date_key(), "222", "Nick")

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/结芬", []),
        )

        prompt = fake_bot.replies[-1]
        self.assertEqual([segment["data"]["qq"] for segment in prompt if segment["type"] == "at"], ["222", "111"])
        self.assertIn("/愿意", "".join(segment["data"].get("text", "") for segment in prompt if segment["type"] == "text"))

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 222},
            CommandContext("/愿意", []),
        )

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_SUCCESS_MESSAGE)
        self.assertFalse(plugin.marriage_proposals_by_requester)
        self.assertFalse(plugin.marriage_proposals_by_target)

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/结芬", []),
        )

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_SUCCESS_MESSAGE)

    async def test_marriage_request_can_only_be_rejected_once_per_day(self) -> None:
        plugin = DailyWifePlugin()
        fake_bot = FakeReplyBot()
        plugin.daily_wives[("100", "111")] = DailyWifeRecord(today_date_key(), "222", "Nick")

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/结芬", []),
        )
        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 222},
            CommandContext("/不愿意", []),
        )

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_FAIL_MESSAGE)

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/结芬", []),
        )

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_FAIL_MESSAGE)
        self.assertFalse(plugin.marriage_proposals_by_requester)
        self.assertFalse(plugin.marriage_proposals_by_target)

    async def test_marriage_request_times_out(self) -> None:
        plugin = DailyWifePlugin()
        fake_bot = FakeReplyBot()
        plugin.daily_wives[("100", "111")] = DailyWifeRecord(today_date_key(), "222", "Nick")

        with patch("plugins.daily_wife.MARRIAGE_RESPONSE_SECONDS", 0.001):
            await plugin.handle(
                fake_bot,
                None,
                {"message_type": "group", "group_id": 100, "user_id": 111},
                CommandContext("/结芬", []),
            )
            await asyncio.sleep(0.05)

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_FAIL_MESSAGE)
        self.assertFalse(plugin.marriage_proposals_by_requester)
        self.assertFalse(plugin.marriage_proposals_by_target)

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/结芬", []),
        )

        self.assertEqual(fake_bot.replies[-1], MARRIAGE_FAIL_MESSAGE)


class CourtshipTest(unittest.IsolatedAsyncioTestCase):
    async def test_courtship_request_can_be_accepted(self) -> None:
        plugin = CourtshipPlugin()
        fake_bot = FakeReplyBot()

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/求偶", ["222"]),
        )

        prompt = fake_bot.replies[-1]
        self.assertEqual([segment["data"]["qq"] for segment in prompt if segment["type"] == "at"], ["222", "111"])
        rendered = "".join(segment["data"].get("text", "") for segment in prompt if segment["type"] == "text")
        self.assertIn("你发情了，请问是否接受求偶", rendered)

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 222},
            CommandContext("/接受求偶", []),
        )

        response = fake_bot.replies[-1]
        self.assertEqual([segment["data"]["qq"] for segment in response if segment["type"] == "at"], ["111"])
        self.assertIn(COURTSHIP_SUCCESS_MESSAGE, "".join(segment["data"].get("text", "") for segment in response if segment["type"] == "text"))
        self.assertFalse(plugin.proposals_by_requester)
        self.assertFalse(plugin.proposals_by_target)

    async def test_courtship_request_can_be_rejected(self) -> None:
        plugin = CourtshipPlugin()
        fake_bot = FakeReplyBot()

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/求偶", ["222"]),
        )
        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 222},
            CommandContext("/拒绝求偶", []),
        )

        response = fake_bot.replies[-1]
        self.assertEqual([segment["data"]["qq"] for segment in response if segment["type"] == "at"], ["111"])
        self.assertIn(COURTSHIP_REJECT_MESSAGE, "".join(segment["data"].get("text", "") for segment in response if segment["type"] == "text"))
        self.assertFalse(plugin.proposals_by_requester)
        self.assertFalse(plugin.proposals_by_target)

    async def test_courtship_requires_target_mention(self) -> None:
        plugin = CourtshipPlugin()
        fake_bot = FakeReplyBot()

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111},
            CommandContext("/求偶", []),
        )

        self.assertEqual(fake_bot.replies[-1], "用法：@bot /求偶 @求偶对象")

    async def test_courtship_rejects_bot_target(self) -> None:
        plugin = CourtshipPlugin()
        fake_bot = FakeReplyBot()

        await plugin.handle(
            fake_bot,
            None,
            {"message_type": "group", "group_id": 100, "user_id": 111, "self_id": 999},
            CommandContext("/求偶", ["999"]),
        )

        self.assertEqual(fake_bot.replies[-1], "不能向机器人求偶。")
        self.assertFalse(plugin.proposals_by_requester)
        self.assertFalse(plugin.proposals_by_target)


if __name__ == "__main__":
    unittest.main()
