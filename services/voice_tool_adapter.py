"""Small, fail-open adaptation for an existing ``tts_speak`` tool call."""

import re
from typing import Any


BASELINE_PERFORMANCE_TAG = "（语速稍快，连续说，停顿很短）"
KNOWN_PERFORMANCE_TAGS = (
    "语速稍快，连续说，停顿很短",
    "轻笑",
    "心虚",
    "委屈",
    "不耐烦",
    "疲惫",
    "撒娇",
    "气声",
    "鼻音",
    "稍慢，语气放软",
    "放慢一点，重点说",
)
_PERFORMANCE_TAG_PATTERN = re.compile(
    r"[（(]\s*(?:" + "|".join(map(re.escape, KNOWN_PERFORMANCE_TAGS)) + r")\s*[）)]"
)


def has_known_performance_tag(text: str) -> bool:
    """Return whether text already contains a supported MiMo control tag."""
    return bool(_PERFORMANCE_TAG_PATTERN.search(text))


def decorate_tts_speak_tool_args(tool_args: Any) -> None:
    """Prepend the baseline tag to plain ``tts_speak`` text in place.

    The caller owns error handling so that a failure cannot interrupt the
    original tool invocation.
    """
    if not isinstance(tool_args, dict):
        return

    text = tool_args.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    if has_known_performance_tag(text):
        return

    tool_args["text"] = f"{BASELINE_PERFORMANCE_TAG}{text}"
