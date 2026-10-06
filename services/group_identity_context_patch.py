"""Compatibility shims for explicit QQ group identity labels in LLM context."""

from __future__ import annotations

import functools
import re
import sys
from typing import Any


_GROUP_CONTEXT_MODULE = "astrbot.builtin_stars.astrbot.group_chat_context"
_PATCH_MARKER = "__xiaoman_identity_context_patch__"
_ORIGINAL_ATTR = "__xiaoman_identity_context_original__"
_REFCOUNT_ATTR = "__xiaoman_identity_context_refcount__"
_TIME_PATTERN = r"\d{2}:\d{2}:\d{2}"

_ANGELHEART_MODULE_TOKEN = "astrbot_plugin_angel_heart"
_ANGELHEART_FORMATTER_SUFFIX = ".core.utils.xml_formatter"
_ANGELHEART_PATCH_MARKER = "__xiaoman_angelheart_identity_patch__"
_ANGELHEART_ORIGINAL_ATTR = "__xiaoman_angelheart_identity_original__"
_AH_NAME_SENTINEL = "__XIAOMAN_NAME__"
_AH_ID_SENTINEL = "__XIAOMAN_QQ__"
_AH_ROLE_SENTINEL = "__XIAOMAN_ROLE__"


def _warn(message: str, *, exc_info: bool = False) -> None:
    """Log through AstrBot when available without making it an import-time dependency."""
    try:
        from astrbot.api import logger

        logger.warning(message, exc_info=exc_info)
    except Exception:
        # Compatibility patches must never prevent the plugin from loading.
        pass


def _clean_identity_value(value: Any) -> str:
    """Keep identity labels single-line and readable for the LLM."""
    if value is None:
        return "未知"
    text = " ".join(str(value).split())
    return text or "未知"


def _extract_group_id_from_chat_id(chat_id: Any) -> str:
    """Extract the group number from an AstrBot unified group origin."""
    text = str(chat_id or "")
    marker = ":GroupMessage:"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip()


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


def _make_angelheart_formatter_wrapper(original):
    """Wrap AngelHeart's formatter without mutating its message dictionaries."""

    @functools.wraps(original)
    def wrapped(msg, *args, **kwargs):
        try:
            if not isinstance(msg, dict):
                return original(msg, *args, **kwargs)

            chat_id = str(msg.get("chat_id", "") or "")
            if (
                msg.get("role") != "user"
                or msg.get("sender_name") == "tool_result"
                or ":GroupMessage:" not in chat_id
                or "sender_name" not in msg
            ):
                return original(msg, *args, **kwargs)

            sender_name = msg.get("sender_name", "成员")
            sender_id = msg.get("sender_id", "Unknown")
            sender_role = msg.get("sender_role") or "群友"
            group_id = _extract_group_id_from_chat_id(chat_id)

            shadow = dict(msg)
            shadow["sender_name"] = _AH_NAME_SENTINEL
            shadow["sender_id"] = _AH_ID_SENTINEL
            shadow["sender_role"] = _AH_ROLE_SENTINEL

            rendered = original(shadow, *args, **kwargs)
            if not isinstance(rendered, str):
                return rendered

            old_header = (
                f"[群友: {_AH_NAME_SENTINEL} "
                f"(ID: {_AH_ID_SENTINEL}, {_AH_ROLE_SENTINEL})]"
            )
            if old_header not in rendered:
                # AngelHeart changed its formatter. Return the untouched original form.
                return original(msg, *args, **kwargs)

            explicit_header = (
                f"[昵称：{_clean_identity_value(sender_name)}"
                f" | QQ号：{_clean_identity_value(sender_id)}"
                f" | 群号：{_clean_identity_value(group_id)}"
                f" | 身份：{_clean_identity_value(sender_role)}]"
            )
            return rendered.replace(old_header, explicit_header, 1)
        except Exception:
            _warn(
                "Xiaoman AngelHeart identity patch failed open for one message",
                exc_info=True,
            )
            return original(msg, *args, **kwargs)

    setattr(wrapped, _ANGELHEART_PATCH_MARKER, True)
    setattr(wrapped, _ANGELHEART_ORIGINAL_ATTR, original)
    return wrapped


def ensure_angelheart_identity_context_patch() -> bool:
    """Patch loaded AngelHeart formatter references after all plugin imports.

    AngelHeart imports format_message_to_text into several modules. This function
    replaces both the canonical formatter and any already-bound references that still
    point to the same original callable. Repeated calls are idempotent.
    """
    try:
        modules = list(sys.modules.items())
        formatter_pairs = []

        for module_name, module in modules:
            if (
                not module
                or _ANGELHEART_MODULE_TOKEN not in module_name
                or not module_name.endswith(_ANGELHEART_FORMATTER_SUFFIX)
            ):
                continue

            current = getattr(module, "format_message_to_text", None)
            if not callable(current):
                continue

            if getattr(current, _ANGELHEART_PATCH_MARKER, False):
                original = getattr(current, _ANGELHEART_ORIGINAL_ATTR, None)
                if callable(original):
                    formatter_pairs.append((original, current))
                continue

            wrapper = _make_angelheart_formatter_wrapper(current)
            setattr(module, "format_message_to_text", wrapper)
            formatter_pairs.append((current, wrapper))

        if not formatter_pairs:
            return False

        for module_name, module in modules:
            if not module or _ANGELHEART_MODULE_TOKEN not in module_name:
                continue

            candidate = getattr(module, "format_message_to_text", None)
            if not callable(candidate):
                continue

            for original, wrapper in formatter_pairs:
                if candidate is original:
                    setattr(module, "format_message_to_text", wrapper)
                    break

        return True
    except Exception:
        _warn("Xiaoman AngelHeart identity patch was not installed", exc_info=True)
        return False


def uninstall_angelheart_identity_context_patch() -> bool:
    """Restore every loaded AngelHeart formatter reference owned by this plugin."""
    restored = False
    try:
        for module_name, module in list(sys.modules.items()):
            if not module or _ANGELHEART_MODULE_TOKEN not in module_name:
                continue

            current = getattr(module, "format_message_to_text", None)
            if not callable(current) or not getattr(
                current,
                _ANGELHEART_PATCH_MARKER,
                False,
            ):
                continue

            original = getattr(current, _ANGELHEART_ORIGINAL_ATTR, None)
            if callable(original):
                setattr(module, "format_message_to_text", original)
                restored = True
        return restored
    except Exception:
        _warn("Xiaoman AngelHeart identity patch uninstall failed", exc_info=True)
        return restored
