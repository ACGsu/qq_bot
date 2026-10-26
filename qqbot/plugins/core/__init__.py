"""Shared native rules. There is no global filter, so passive matchers remain usable."""
from dataclasses import dataclass
from nonebot import get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel
from infrastructure.runtime import runtime

__plugin_meta__ = PluginMetadata(name="公共规则", description="严格 @、未知指令兜底和可控自检", usage="@bot /自检（需开启且为超级用户）")

class Config(BaseModel):
    core_selftest_enabled: bool = False


@dataclass(frozen=True)
class CommandContext:
    text: str
    mentions: tuple[str, ...]


def command_context_after_real_mention(event: MessageEvent) -> CommandContext | None:
    original = event.original_message
    for index, segment in enumerate(original):
        if segment.type == "at" and str(segment.data.get("qq")) == str(event.self_id):
            remaining = original[index + 1:]
            text = "".join(str(s.data.get("text", "")) for s in remaining if s.type == "text").strip()
            mentions = tuple(
                str(s.data.get("qq")) for s in remaining
                if s.type == "at" and str(s.data.get("qq", "")).lower() != "all"
            )
            return CommandContext(text, mentions)
    return None


def command_after_real_mention(event: MessageEvent) -> str | None:
    context = command_context_after_real_mention(event)
    return context.text if context else None

async def real_mention(event: MessageEvent) -> bool:
    return command_after_real_mention(event) is not None

strict_mention = Rule(real_mention)

async def selftest_command(event: MessageEvent) -> bool:
    return get_plugin_config(Config).core_selftest_enabled and command_after_real_mention(event) == "/自检"

selftest = on_message(rule=Rule(selftest_command), permission=SUPERUSER, priority=10, block=True)

@selftest.handle()
async def handle_selftest(bot: Bot):
    await selftest.finish(f"NoneBot 运行正常；Bot {bot.self_id}；配置 {runtime.source_version[:12]}")

fallback = on_message(rule=strict_mention, priority=99, block=True)

@fallback.handle()
async def handle_unknown():
    message = Message("没有该指令喵~")
    image = runtime.config.assets_root / "command_not_found.jpg"
    if image.is_file():
        message += MessageSegment.image(image.read_bytes())
    await fallback.finish(message)
