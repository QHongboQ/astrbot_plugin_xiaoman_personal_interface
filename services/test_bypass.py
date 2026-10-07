"""Request-local administrator test-mode isolation helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from astrbot.core.agent.message import TextPart

TEST_ADMIN_UID = "979675497"
ANGELHEART_PLUGIN_NAME = "astrbot_plugin_angel_heart"
ANGELHEART_HANDLER_NAME = "smart_reply_handler"
TEST_GUIDANCE = (
    "当前为管理员测试放行模式。"
    "当前消息来自已授权的测试管理员，应视为功能与交互测试请求。"
    "在功能存在且请求本身允许执行时，不要因为角色性格、害羞、嘴硬、"
    "重复话题、关系阶段或社交理由拒绝，也不要要求管理员额外说明这是测试。"
    "根据管理员当前自然语言请求正常使用相应工具或能力完成测试，"
    "同时保持林小满原本的说话语气。"
    "测试模式不代表必须调用工具，仅在当前请求确实对应某项能力时使用相应工具。"
)

_TEST_COMMAND = re.compile(r"^/xiaoman_test(?:\s|$)", re.IGNORECASE)


def get_sender_uid(event: Any) -> str:
    """Return AstrBot's sender UID without changing the event."""
    try:
        return str(event.get_sender_id() or "")
    except Exception:
        return ""


def get_umo(event: Any) -> str:
    """Return the event's unified message origin, or an empty string."""
    try:
        return str(event.unified_msg_origin or "")
    except Exception:
        return ""


def is_authorized_test_umo(event: Any, enabled_umos: set[str]) -> bool:
    return get_sender_uid(event) == TEST_ADMIN_UID and get_umo(event) in enabled_umos


def is_test_command_event(event: Any) -> bool:
    """Keep the mode-management command itself outside the test bypass."""
    try:
        message = str(event.message_str or "").lstrip()
    except Exception:
        return False
    return _TEST_COMMAND.match(message) is not None


def is_angelheart_ingress_handler(handler: Any) -> bool | None:
    """Match only AngelHeart's ordinary ingress handler by module and name."""
    try:
        module_path = handler.handler_module_path
        handler_name = handler.handler_name
        if not isinstance(module_path, str) or not isinstance(handler_name, str):
            return None
        module_parts = module_path.split(".")
        return (
            handler_name == ANGELHEART_HANDLER_NAME
            and ANGELHEART_PLUGIN_NAME in module_parts
        )
    except Exception:
        return None


def _active_plugin_names(plugin_context: Any) -> list[str] | None:
    getter = getattr(plugin_context, "get_all_stars", None)
    if not callable(getter):
        return None
    try:
        stars = getter()
        if isinstance(stars, (str, bytes, dict)) or not isinstance(stars, Iterable):
            return None
        names: list[str] = []
        for star in stars:
            activated = getattr(star, "activated", None)
            if not isinstance(activated, bool):
                return None
            if not activated:
                continue
            name = getattr(star, "name", None)
            if not isinstance(name, str):
                return None
            if name and name != ANGELHEART_PLUGIN_NAME:
                names.append(name)
        return names
    except Exception:
        return None


def apply_admin_test_bypass(
    event: Any,
    enabled_umos: set[str],
    plugin_context: Any,
) -> bool:
    """Exclude AngelHeart ingress and hooks atomically for this event only.

    Any unsafe or unavailable runtime structure fails open without leaving a
    partial bypass behind.
    """
    if not is_authorized_test_umo(event, enabled_umos) or is_test_command_event(event):
        return False

    try:
        get_extra = getattr(event, "get_extra", None)
        if not callable(get_extra) or not hasattr(event, "plugins_name"):
            return False
        handlers = get_extra("activated_handlers")
        if not isinstance(handlers, list) or not hasattr(event, "is_at_or_wake_command"):
            return False

        original_handlers = list(handlers)
        original_plugins = event.plugins_name
        original_wake = event.is_at_or_wake_command

        if original_plugins is None or original_plugins == ["*"]:
            replacement_plugins = _active_plugin_names(plugin_context)
            if replacement_plugins is None:
                return False
        elif isinstance(original_plugins, list) and all(
            isinstance(name, str) for name in original_plugins
        ):
            replacement_plugins = [
                name for name in original_plugins if name != ANGELHEART_PLUGIN_NAME
            ]
        else:
            return False

        replacement_handlers = []
        for handler in original_handlers:
            is_target = is_angelheart_ingress_handler(handler)
            if is_target is None:
                return False
            if not is_target:
                replacement_handlers.append(handler)

        try:
            event.plugins_name = replacement_plugins
            handlers[:] = replacement_handlers
            event.is_at_or_wake_command = True
        except Exception:
            event.plugins_name = original_plugins
            handlers[:] = original_handlers
            event.is_at_or_wake_command = original_wake
            return False
        return True
    except Exception:
        return False


def inject_test_guidance(event: Any, enabled_umos: set[str], req: Any) -> bool:
    """Append static request-local guidance only for an enabled test UMO."""
    if not is_authorized_test_umo(event, enabled_umos) or is_test_command_event(event):
        return False
    try:
        parts = req.extra_user_content_parts
        if not isinstance(parts, list) or any(
            (isinstance(part, TextPart) and part.text == TEST_GUIDANCE)
            or (
                isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("text") == TEST_GUIDANCE
            )
            or part == TEST_GUIDANCE
            for part in parts
        ):
            return False
        parts.append(TextPart(text=TEST_GUIDANCE).mark_as_temp())
        return True
    except Exception:
        return False


def update_test_mode(event: Any, enabled_umos: set[str], action: str) -> str:
    """Apply an administrator command after explicit UID verification."""
    if get_sender_uid(event) != TEST_ADMIN_UID:
        return "无权限"
    umo = get_umo(event)
    if not umo:
        return "无权限"

    normalized_action = str(action or "status").strip().lower()
    if normalized_action == "on":
        enabled_umos.add(umo)
        return "小满测试放行模式：已开启（仅当前会话）"
    if normalized_action == "off":
        enabled_umos.discard(umo)
        return "小满测试放行模式：已关闭"
    if normalized_action == "status":
        state = "已开启" if umo in enabled_umos else "已关闭"
        return f"小满测试放行模式：{state}"
    return "用法：/xiaoman_test on|off|status"
