"""Compatibility shim for explicit QQ group identity labels in AstrBot context."""

from __future__ import annotations

import functools
import re
from typing import Any


_GROUP_CONTEXT_MODULE = "astrbot.builtin_stars.astrbot.group_chat_context"
_PATCH_MARKER = "__xiaoman_identity_context_patch__"
_ORIGINAL_ATTR = "__xiaoman_identity_context_original__"
_REFCOUNT_ATTR = "__xiaoman_identity_context_refcount__"
_TIME_PATTERN = r"\d{2}:\d{2}:\d{2}"


def _warn(message: str, *, exc_info: bool = False) -> None:
    """Log through AstrBot when available without making it an import-time dependency."""
    try:
        from astrbot.api import logger

        logger.warning(message, exc_info=exc_info)
    except Exception:
        # This compatibility layer must never prevent the plugin from loading.
        pass


def _clean_identity_value(value: Any) -> str:
    """Keep identity labels single-line and readable for the LLM."""
    if value is None:
        return "未知"
    text = " ".join(str(value).split())
    return text or "未知"


def rewrite_group_context_record(rendered: str, event: Any) -> str:
    """Replace AstrBot's ambiguous nickname/time prefix with explicit labels.

    The message body is preserved byte-for-byte after the original prefix. If AstrBot
    changes its formatter or the event does not expose the expected fields, the input
    is returned unchanged.
    """
    if not isinstance(rendered, str):
        return rendered

    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    nickname = getattr(sender, "nickname", None)
    if nickname is None:
        return rendered

    nickname_text = str(nickname)
    prefix_pattern = re.compile(
        rf"^\[{re.escape(nickname_text)}/(?P<time>{_TIME_PATTERN})\]: "
    )
    match = prefix_pattern.match(rendered)
    if not match:
        return rendered

    try:
        sender_id = event.get_sender_id()
    except Exception:
        sender_id = getattr(sender, "user_id", None)

    try:
        group_id = event.get_group_id()
    except Exception:
        group_id = getattr(message_obj, "group_id", None)

    explicit_prefix = (
        f"[昵称：{_clean_identity_value(nickname)}"
        f" | QQ号：{_clean_identity_value(sender_id)}"
        f" | 群号：{_clean_identity_value(group_id)}"
        f" | 时间：{match.group('time')}]： "
    )
    return explicit_prefix + rendered[match.end() :]


def install_group_identity_context_patch() -> bool:
    """Patch only AstrBot's group-history formatter, leaving source files untouched."""
    try:
        module = __import__(_GROUP_CONTEXT_MODULE, fromlist=["GroupChatContext"])
        group_context_cls = module.GroupChatContext
        current = getattr(group_context_cls, "_format_message", None)
        if not callable(current):
            _warn("Xiaoman group identity patch skipped: formatter is unavailable")
            return False

        if getattr(current, _PATCH_MARKER, False):
            refcount = int(getattr(group_context_cls, _REFCOUNT_ATTR, 0) or 0)
            setattr(group_context_cls, _REFCOUNT_ATTR, refcount + 1)
            return True

        original = current

        @functools.wraps(original)
        async def wrapped(instance, event, cfg):
            rendered = await original(instance, event, cfg)
            try:
                return rewrite_group_context_record(rendered, event)
            except Exception:
                _warn(
                    "Xiaoman group identity patch failed open for one message",
                    exc_info=True,
                )
                return rendered

        setattr(wrapped, _PATCH_MARKER, True)
        setattr(group_context_cls, _ORIGINAL_ATTR, original)
        setattr(group_context_cls, _REFCOUNT_ATTR, 1)
        setattr(group_context_cls, "_format_message", wrapped)
        return True
    except Exception:
        _warn("Xiaoman group identity patch was not installed", exc_info=True)
        return False


def uninstall_group_identity_context_patch() -> bool:
    """Release one patch owner and restore AstrBot's formatter when the last exits."""
    try:
        module = __import__(_GROUP_CONTEXT_MODULE, fromlist=["GroupChatContext"])
        group_context_cls = module.GroupChatContext
        current = getattr(group_context_cls, "_format_message", None)
        if not callable(current) or not getattr(current, _PATCH_MARKER, False):
            return False

        refcount = int(getattr(group_context_cls, _REFCOUNT_ATTR, 1) or 1)
        if refcount > 1:
            setattr(group_context_cls, _REFCOUNT_ATTR, refcount - 1)
            return True

        original = getattr(group_context_cls, _ORIGINAL_ATTR, None)
        if not callable(original):
            _warn(
                "Xiaoman group identity patch could not restore the original formatter"
            )
            return False

        setattr(group_context_cls, "_format_message", original)
        for attr_name in (_ORIGINAL_ATTR, _REFCOUNT_ATTR):
            try:
                delattr(group_context_cls, attr_name)
            except AttributeError:
                pass
        return True
    except Exception:
        _warn("Xiaoman group identity patch uninstall failed", exc_info=True)
        return False
