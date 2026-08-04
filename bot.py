import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from plugin_control import PLUGIN_DEFINITIONS, load_plugin_config
from plugins.auto_emoji import AutoEmojiPlugin
from plugins.basic import BasicCommandPlugin, dispatch_command
from plugins.common import (
    BotPlugin,
    CHINA_TZ,
    COMMAND_NOT_FOUND_IMAGE_PATH,
    NOT_FOUND_IMAGE_PATH,
    CommandContext,
    at_segment,
    command_argument,
    extract_command_after_mention,
    extract_command_context_after_mention,
    image_segment_from_file,
    qq_avatar_segment,
    text_segment,
    today_date_key,
)
from plugins.courtship import (
    CourtshipPlugin,
    CourtshipProposal,
    COURTSHIP_REJECT_MESSAGE,
    COURTSHIP_SUCCESS_MESSAGE,
)
from plugins.daily_wife import (
    DailyWifePlugin,
    DailyWifeRecord,
    format_daily_wife_existing,
    format_daily_wife_new,
    member_display_name,
    wife_candidates,
)
from plugins.novel import (
    LINOVELIB_BASE_URL,
    NovelCatalog,
    NovelChapter,
    NovelInfo,
    NovelPlugin,
    NovelVolume,
    extract_novel_id,
    fetch_text,
    find_best_novel,
    normalize_title,
    parse_chinese_number,
    parse_novel_catalog,
    parse_novel_info_from_page,
    parse_novel_links,
    parse_ordinal_command,
    strip_tags,
)
from plugins.rps import (
    BAN_SECONDS,
    RPS_BEATS,
    RPS_CHOICES,
    RockPaperScissorsPlugin,
    RpsResult,
    format_rps_result,
    play_rock_paper_scissors,
)
from plugins.song import (
    SONG_DOWNLOAD_DIR,
    SONG_MAX_DOWNLOAD_BYTES,
    SONG_SEARCH_LIMIT,
    SONG_SESSION_SECONDS,
    XIAGEBA_BASE_URL,
    QuarkDownloadError,
    SongDetails,
    SongDownload,
    SongPlugin,
    SongResult,
    SongSearchSession,
    choose_quark_audio_file,
    choose_song_source,
    convert_song_to_mp3,
    download_quark_share_source,
    download_song_source,
    fetch_json,
    fetch_song_details,
    format_song_details,
    format_song_search_results,
    is_quark_share_url,
    is_share_page_url,
    parse_song_details_response,
    parse_song_search_response,
    parse_song_selection,
    quark_api_request,
    quark_share_files,
    quark_share_id,
    safe_song_filename,
    search_songs,
    wait_for_quark_task,
)
from plugins.summary import (
    GroupSummaryPlugin,
    extract_history_messages,
    message_to_text,
    summarize_group_messages,
)
from plugins.websocket import NativeWebSocketClient, NativeWebSocketConnection, WS_GUID


LOGGER = logging.getLogger("qq-bot")


@dataclass(frozen=True)
class BotConfig:
    websocket_mode: str
    ws_url: str
    access_token: str | None
    bot_qq: str | None
    listen_host: str
    listen_port: int
    reconnect_seconds: float
    quark_cookie: str | None

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            websocket_mode=os.getenv("WEBSOCKET_MODE", "client").lower(),
            ws_url=os.getenv("NAPCAT_WS_URL", "ws://napcat:3001"),
            access_token=os.getenv("NAPCAT_ACCESS_TOKEN") or None,
            bot_qq=os.getenv("BOT_QQ") or None,
            listen_host=os.getenv("LISTEN_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("LISTEN_PORT", "8080")),
            reconnect_seconds=float(os.getenv("RECONNECT_SECONDS", "5")),
            quark_cookie=os.getenv("QUARK_COOKIE") or None,
        )


class NapCatBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.logger = LOGGER
        self._stop = asyncio.Event()
        self._action_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.enabled_plugin_ids: tuple[str, ...] = ()
        self.plugins = self._build_plugins()

    def _build_plugins(self) -> list[BotPlugin]:
        factories = {
            "auto_emoji": AutoEmojiPlugin,
            "rps": RockPaperScissorsPlugin,
            "daily_wife": DailyWifePlugin,
            "courtship": CourtshipPlugin,
            "song": SongPlugin,
            "novel": NovelPlugin,
            "summary": GroupSummaryPlugin,
            "basic": BasicCommandPlugin,
        }
        plugin_config = load_plugin_config()
        self.enabled_plugin_ids = plugin_config.enabled_plugin_ids
        enabled_plugin_ids = set(self.enabled_plugin_ids)
        configured_plugin_ids = {"auto_emoji", "summary"}
        plugins = [
            factories[plugin.plugin_id](plugin_config.plugin_settings.get(plugin.plugin_id, {}))
            if plugin.plugin_id in configured_plugin_ids
            else factories[plugin.plugin_id]()
            for plugin in PLUGIN_DEFINITIONS
            if plugin.plugin_id in enabled_plugin_ids and plugin.plugin_id in factories
        ]
        enabled_names = ", ".join(plugin.name for plugin in PLUGIN_DEFINITIONS if plugin.plugin_id in enabled_plugin_ids)
        LOGGER.info("Enabled plugins: %s", enabled_names or "none")
        return plugins

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        if self.config.websocket_mode == "server":
            await self._run_server()
            return
        if self.config.websocket_mode != "client":
            raise ValueError("WEBSOCKET_MODE must be client or server")

        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("WebSocket connection failed")

            if not self._stop.is_set():
                LOGGER.info("Reconnect in %.1f seconds", self.config.reconnect_seconds)
                await asyncio.sleep(self.config.reconnect_seconds)

    async def _run_once(self) -> None:
        headers = {}
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"

        async with NativeWebSocketClient(self.config.ws_url, headers=headers) as websocket:
            LOGGER.info("Connected to %s", self.config.ws_url)
            while not self._stop.is_set():
                raw = await websocket.recv()
                self._spawn_event_task(websocket, raw)

    async def _run_server(self) -> None:
        server = await asyncio.start_server(self._handle_reverse_connection, self.config.listen_host, self.config.listen_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        LOGGER.info("Listening for NapCat reverse WebSocket on %s", sockets)
        async with server:
            await self._stop.wait()
            server.close()
            await server.wait_closed()

    async def _handle_reverse_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            websocket = await NativeWebSocketConnection.accept(reader, writer, self.config.access_token)
            LOGGER.info("NapCat reverse WebSocket connected from %s", peer)
            while not self._stop.is_set():
                raw = await websocket.recv()
                self._spawn_event_task(websocket, raw)
        except asyncio.IncompleteReadError:
            LOGGER.info("NapCat reverse WebSocket disconnected from %s", peer)
        except Exception:
            LOGGER.exception("NapCat reverse WebSocket failed from %s", peer)
        finally:
            writer.close()
            await writer.wait_closed()

    def _spawn_event_task(self, websocket: Any, raw: str | bytes) -> None:
        task = asyncio.create_task(self._handle_raw_event(websocket, raw))
        task.add_done_callback(self._log_event_task_error)

    def _log_event_task_error(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("Message handling task failed")

    async def _handle_raw_event(self, websocket: Any, raw: str | bytes) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Ignore non-json message: %r", raw)
            return

        echo = event.get("echo")
        if echo:
            waiter = self._action_waiters.pop(str(echo), None)
            if waiter and not waiter.done():
                waiter.set_result(event)
                return

        if event.get("post_type") != "message":
            return

        for plugin in self.plugins:
            handle_event = getattr(plugin, "handle_event", None)
            if handle_event:
                await handle_event(self, websocket, event)

        bot_qq = self.config.bot_qq or event.get("self_id")
        context = extract_command_context_after_mention(event.get("message"), str(bot_qq) if bot_qq else None)
        if context is None:
            return

        command_name = context.text.split(maxsplit=1)[0] if context.text else ""
        LOGGER.info(
            "Received command %s from %s user %s",
            command_name or "<empty>",
            event.get("message_type"),
            event.get("user_id"),
        )
        for plugin in self.plugins:
            if plugin.matches(command_name, context):
                await plugin.handle(self, websocket, event, context)
                return

        message = [text_segment("没有该指令喵~")]
        if COMMAND_NOT_FOUND_IMAGE_PATH.exists():
            message.append(image_segment_from_file(COMMAND_NOT_FOUND_IMAGE_PATH))
        await self._send_reply(websocket, event, message)

    async def _get_group_member_list(self, websocket: Any, group_id: str) -> list[dict[str, Any]]:
        response = await self._send_action_request(
            websocket,
            "get_group_member_list",
            {"group_id": int(group_id)},
            timeout=15,
        )
        data = response.get("data") or []
        if not isinstance(data, list):
            raise RuntimeError("get_group_member_list returned invalid data")
        return [member for member in data if isinstance(member, dict)]

    async def _ban_group_member(self, websocket: Any, group_id: str, user_id: str, duration: int) -> None:
        await self._send_action(
            websocket,
            "set_group_ban",
            {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "duration": duration,
            },
        )

    async def _send_reply(self, websocket: Any, event: dict[str, Any], message: str | list[dict[str, Any]]) -> None:
        message_type = event.get("message_type")
        message_segments = [text_segment(message)] if isinstance(message, str) else message
        params: dict[str, Any] = {
            "message_type": message_type,
            "message": message_segments,
        }

        if message_type == "group":
            params["group_id"] = event["group_id"]
        elif message_type == "private":
            params["user_id"] = event["user_id"]
        else:
            LOGGER.warning("Unsupported message_type: %s", message_type)
            return

        await self._send_action(websocket, "send_msg", params)

    async def _send_action(self, websocket: Any, action: str, params: dict[str, Any]) -> None:
        await websocket.send(
            json.dumps(
                {
                    "action": action,
                    "params": params,
                    "echo": f"{action}:{int(time.time())}:{uuid4().hex}",
                },
                ensure_ascii=False,
            )
        )

    async def _send_action_request(
        self,
        websocket: Any,
        action: str,
        params: dict[str, Any],
        timeout: float = 10,
    ) -> dict[str, Any]:
        echo = f"{action}:{int(time.time())}:{uuid4().hex}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._action_waiters[echo] = future
        try:
            await websocket.send(
                json.dumps(
                    {
                        "action": action,
                        "params": params,
                        "echo": echo,
                    },
                    ensure_ascii=False,
                )
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._action_waiters.pop(echo, None)

        retcode = response.get("retcode")
        status = response.get("status")
        if retcode not in (0, None) or status not in ("ok", "async", None):
            raise RuntimeError(f"OneBot action {action} failed: retcode={retcode}, status={status}")
        return response


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bot = NapCatBot(BotConfig.from_env())

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            running_loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: bot.stop())

    await bot.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
