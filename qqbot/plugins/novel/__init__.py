"""Native novel search and ordinal link commands."""
import logging

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from infrastructure.runtime import runtime
from infrastructure.onebot import command_argument
from plugins.core import command_context_after_real_mention
from services.novel import (
    NovelCatalog,
    lookup_catalog,
    parse_ordinal_command,
)

LOGGER = logging.getLogger("qq-bot")
novel_sessions: dict[str, NovelCatalog] = {}

__plugin_meta__ = PluginMetadata(
    name="小说链接", description="查询轻小说目录并返回章节或卷阅读链接",
    usage="@bot /novel 小说名；@bot /第1章；@bot /第1卷",
)


def _command_text(event: MessageEvent) -> str:
    context = command_context_after_real_mention(event)
    return context.text if context else ""


async def novel_command(event: MessageEvent) -> bool:
    text = _command_text(event)
    command = text.split(maxsplit=1)[0] if text else ""
    return command == "/novel" or parse_ordinal_command(command) is not None


novel = on_message(rule=Rule(novel_command), priority=20, block=True)


@novel.handle()
async def handle_novel(event: MessageEvent) -> None:
    text = _command_text(event)
    command = text.split(maxsplit=1)[0] if text else ""
    if command == "/novel":
        await _handle_lookup(event, text)
        return
    await _handle_ordinal(event, command)


async def _handle_lookup(event: MessageEvent, text: str) -> None:
    name = command_argument(text)
    if not name:
        await novel.finish("用法：@bot /novel 小说名字")
    try:
        catalog = await lookup_catalog(get_driver(), name)
    except Exception:
        LOGGER.exception("Linovelib novel lookup failed")
        await _send_not_found()
        return
    if catalog is None:
        await _send_not_found()
        return
    novel_sessions[str(event.user_id)] = catalog
    await novel.finish(
        f"已找到：{catalog.novel.title}\n"
        f"目录：{catalog.novel.catalog_url}\n"
        f"共 {len(catalog.volumes)} 卷，{len(catalog.chapters)} 章。\n"
        "接下来可输入：@bot /第1卷 或 @bot /第1章"
    )


async def _handle_ordinal(event: MessageEvent, command: str) -> None:
    parsed = parse_ordinal_command(command)
    if parsed is None:
        return
    catalog = novel_sessions.get(str(event.user_id))
    if catalog is None:
        await novel.finish("请先输入：@bot /novel 小说名字")
    kind, index = parsed
    if kind == "卷":
        if index > len(catalog.volumes):
            await novel.finish(f"这本书暂时没有第{index}卷。")
        volume = catalog.volumes[index - 1]
        await novel.finish(f"{catalog.novel.title}\n第{index}卷：{volume.title}\n{volume.url}")
    if index > len(catalog.chapters):
        await novel.finish(f"这本书暂时没有第{index}章。")
    chapter = catalog.chapters[index - 1]
    await novel.finish(
        f"{catalog.novel.title}\n第{index}章：{chapter.title}\n"
        f"所属卷：{chapter.volume_title}\n{chapter.url}"
    )


async def _send_not_found() -> None:
    message = Message("私密马赛，暂时没有该小说呢")
    image = runtime.config.assets_root / "not_found.jpg"
    if image.is_file():
        message += MessageSegment.image(image.read_bytes())
    await novel.finish(message)


def reset_state() -> None:
    novel_sessions.clear()


@get_driver().on_shutdown
async def shutdown() -> None:
    reset_state()
