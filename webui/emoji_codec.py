"""Unicode emoji to NapCat decimal IDs; no arbitrary text or QQ-face guessing.

NapCat packages/napcat-core/apis/msg.ts setEmojiLike selects Unicode type 2
for IDs longer than three decimal digits; shorter IDs select QQ faces.
Source checked 2026-10-01: https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/apis/msg.ts
Conversion is not a guarantee that a particular QQ/NapCat version accepts it.
"""
import re
import regex

_EMOJI = regex.compile(r"\A\p{Emoji}\Z")
_COMPONENT = regex.compile(r"\A\p{Emoji_Component}\Z")
_PRESENTATION = regex.compile(r"\A\p{Emoji_Presentation}\Z")


def is_single_emoji(value: str) -> bool:
    # Reject digits, regional indicators, skin tones, hair components and short
    # codepoints (e.g. copyright) that NapCat would interpret as QQ face IDs.
    return (len(value) == 1 and ord(value) >= 1000
            and bool(_EMOJI.fullmatch(value)) and not _COMPONENT.fullmatch(value))


def encode(text: str) -> list[str]:
    result = []
    for cluster in regex.findall(r"\X", text):
        if cluster.isspace() or cluster in {",", "，", ";", "；"}:
            continue
        normalized = cluster.removesuffix("\ufe0f")
        if not is_single_emoji(normalized):
            raise ValueError("不支持该字符或组合表情；可粘贴 😀 😂 👍 ❤️ 🔥 等单码点 Emoji。肤色、旗帜、家庭等组合、QQ 专属表情及文字／数字编码暂不支持")
        value = str(ord(normalized))
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("请至少输入一个受支持的表情")
    return result


def tokens(raw: str) -> list[str]:
    return list(dict.fromkeys(x for x in re.split(r"[,，;；\s]+", raw.strip()) if x))


def preview(ids: list[str]) -> list[dict]:
    result = []
    for value in ids:
        label = "旧版表情（保留）"
        legacy = True
        # Bound integer parsing, require canonical decimal IDs and do not
        # reinterpret QQ faces or arbitrary historical IDs as Unicode text.
        if re.fullmatch(r"[1-9][0-9]{3,6}", value):
            code = int(value)
            if code <= 0x10ffff and is_single_emoji(chr(code)):
                label = chr(code)
                if not _PRESENTATION.fullmatch(label):
                    label += "\ufe0f"
                legacy = False
        result.append({"id": value, "label": label, "legacy": legacy})
    return result
