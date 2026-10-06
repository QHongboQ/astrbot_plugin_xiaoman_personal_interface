"""Per-event routing between Xiaoman's visual requests and Airi's gallery browse handler."""

import re

from astrbot.api import logger


AIRI_PLUGIN_SEGMENT = "astrbot_plugin_airi_gallery"
AIRI_GENERIC_HANDLER_NAME = "handle_gallery_message"

_NORMALIZE = re.compile(r"[\s\W_]+", re.UNICODE)
_VIEW_ACTION = re.compile(r"看看|看一看|看一下|看下|瞅瞅|瞧瞧|看")
_REQUEST_ACTION = re.compile(
    r"我(?:想要|想|要)|想(?:要|看)|要(?:看|照片|自拍|相片|写真|图片|图像)"
    r"|能(?:不能)?(?:让我|给我|发|看|来|有)|可不可以(?:让我|给我|发|看|来|有)"
    r"|可以(?:让我|给我|发|看|来|有)|请(?:让我|给我|发|看)"
    r"|让我|给(?:我(?:一张|张)?|一张|张)|分享|发(?:给我|我|一张|张|来)"
    r"|传(?:给我|我|一张|张)|送(?:给我|我|一张|张)|来(?:一张|张|点)|有没(?:有)?"
    r"|有(?:照片|自拍|相片|写真|图片|图像)"
    r"|(?:照片|自拍|相片|写真|图片|图像)有(?:吗|没有)?$"
)
_VISUAL_OBJECT = re.compile(
    r"照片|自拍|相片|写真|图片|图像|影像|长什么样|长啥样|什么样子|啥样|长相|模样|外貌"
)
_PHOTO_OBJECT = re.compile(r"照片|自拍|相片|写真|图片|图像|影像")
_SELF_TARGET = re.compile(r"你(?:自己|本人)?|林小满|小满")
_YOU_AS_ACTOR = re.compile(
    r"^你(?:自己)?(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧|看)"
)
_APPEARANCE_QUESTION = re.compile(r"长什么样|长啥样|什么样子|啥样|长相|模样|外貌|长得")
_DIRECT_VIEW_TARGET = re.compile(
    r"(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧|看)"
    r"(?:你(?:自己|本人)?|林小满|小满)(?:吗|呢|呀)?$"
)
_FOLLOW_UP = re.compile(r"(?:照片|自拍|相片|写真|图片|图像)(?:呢|呀|吗|可以吗|行吗)$")
_BARE_VISUAL_LOOK = re.compile(
    r"(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧|看)"
    r"(?:你的?|自己|本人)?(?:照片|自拍|相片|写真)(?:呢|呀|吗)?$"
)
_USER_IMAGE_REFERENCE = re.compile(
    r"(?:这|那)(?:张|个|幅)?(?:照片|图片|相片|图像|图|截图)"
    r"|我(?:刚才|刚)?(?:发(?:给你)?|上传|传)的(?:照片|图片|相片|图像|图|截图)"
    r"|我(?:这|那)(?:张|个)?(?:照片|图片|相片|图像|图|自拍)"
    r"|我(?:的|拍的|自拍的|自己拍的)(?:照片|图片|相片|图像|图|自拍)"
    r"|我自拍(?:照|图|照片)?"
    r"|你(?:刚才|刚)?发(?:给我)?的(?:照片|图片|相片|图像|图|截图)"
    r"|(?:上面|刚才)(?:那|这)?(?:张|个|幅)?(?:照片|图片|相片|图像|图|截图)"
)
_ARBITRARY_IMAGE = re.compile(r"商品(?:主图|照片|图片|图|外观|详情)|截图|文件")
_IMAGE_ANALYSIS = re.compile(r"分析|识别|解释|总结|提取|ocr", re.IGNORECASE)


def _normalize_message(message: str) -> str:
    """Normalize punctuation and whitespace while preserving Chinese/digit tokens."""
    return _NORMALIZE.sub("", message)


def _is_airi_browse_shaped(message: str, has_self_target: bool, has_request: bool) -> bool:
    """Keep bare Airi look/category syntax on its original route."""
    if message.startswith("/"):
        return True
    if not message.startswith("看") or has_self_target or has_request:
        return False
    if _BARE_VISUAL_LOOK.fullmatch(message):
        return False
    # A bare “看看<category>” is Airi's no-prefix browse grammar. Conversational
    # follow-ups such as “照片呢” are not command-shaped and remain eligible.
    return not bool(_FOLLOW_UP.search(message))


def _is_xiaoman_visual_request(message: str, *, directed: bool) -> bool:
    """Classify a visual request compositionally; directedness supplies an implicit target."""
    if not directed or not message:
        return False
    if _USER_IMAGE_REFERENCE.search(message) or _ARBITRARY_IMAGE.search(message):
        return False
    if _IMAGE_ANALYSIS.search(message) and _VISUAL_OBJECT.search(message):
        return False

    has_target = bool(_SELF_TARGET.search(message))
    has_request = bool(_REQUEST_ACTION.search(message))
    if _is_airi_browse_shaped(message, has_target, has_request):
        return False

    has_view = bool(_VIEW_ACTION.search(message))
    has_visual_object = bool(_VISUAL_OBJECT.search(message))
    has_photo_object = bool(_PHOTO_OBJECT.search(message))
    asks_appearance = bool(_APPEARANCE_QUESTION.search(message))

    # Explicit self-targets support concise “看看你/小满” and possessive-photo asks.
    you_is_actor = bool(_YOU_AS_ACTOR.search(message))
    if has_target and asks_appearance:
        return True
    if (
        has_target
        and has_view
        and not you_is_actor
        and (has_visual_object or _DIRECT_VIEW_TARGET.search(message))
    ):
        return True
    if has_target and _DIRECT_VIEW_TARGET.search(message):
        return True
    if has_target and has_request and has_photo_object:
        return True
    if has_view and has_photo_object and _BARE_VISUAL_LOOK.fullmatch(message):
        return True

    # After wake-prefix/@ normalization, a directed request may omit its subject.
    if has_request and has_visual_object:
        return True
    if has_photo_object and _FOLLOW_UP.search(message):
        return True
    # “长什么样？” is itself an appearance inquiry in a directed conversation.
    return asks_appearance


def is_directed_look_request(event) -> bool:
    """Return whether this directed event requests Xiaoman's own visual appearance."""
    if not bool(getattr(event, "is_at_or_wake_command", False)):
        return False
    original = str(getattr(event, "message_str", "") or "").strip()
    if original.startswith("/"):
        return False
    message = _normalize_message(original)
    return _is_xiaoman_visual_request(message, directed=True)


def is_airi_generic_browse_handler(handler) -> bool:
    """Match only the official Airi generic natural-message handler metadata."""
    if getattr(handler, "handler_name", None) != AIRI_GENERIC_HANDLER_NAME:
        return False
    module_path = str(getattr(handler, "handler_module_path", "") or "")
    return AIRI_PLUGIN_SEGMENT in module_path.split(".")


def guard_directed_look_request(event) -> bool:
    """Remove Airi's generic handler in place for a classified Xiaoman visual request.

    AstrBot retains this activated-handler list while iterating it, so slice
    assignment preserves the list identity. Inspection failures fail open.
    """
    if not is_directed_look_request(event):
        return False

    try:
        activated_handlers = event.get_extra("activated_handlers")
    except Exception:
        logger.warning("Xiaoman gallery route guard could not inspect handlers")
        return False

    if not isinstance(activated_handlers, list):
        logger.warning("Xiaoman gallery route guard received no handler list")
        return False

    retained_handlers = [
        handler
        for handler in activated_handlers
        if not is_airi_generic_browse_handler(handler)
    ]
    if len(retained_handlers) == len(activated_handlers):
        return False

    activated_handlers[:] = retained_handlers
    logger.debug("Xiaoman gallery route guard bypassed Airi generic browse handler")
    return True
