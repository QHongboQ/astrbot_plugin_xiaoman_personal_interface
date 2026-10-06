"""Per-event guard for Airi Gallery's generic no-prefix browse handler."""

import re

from astrbot.api import logger


AIRI_PLUGIN_SEGMENT = "astrbot_plugin_airi_gallery"
AIRI_GENERIC_HANDLER_NAME = "handle_gallery_message"

_PHOTO_REQUEST_PATTERNS = (
    # Explicit photo nouns: "我要看看你的照片", "给我看看照片".
    re.compile(
        r"(?:我(?:想|要|想要)?|想|要|让我|给我|能不能|可以|能否|麻烦)?"
        r"(?:看(?:看|一下|下|一眼)?|瞅瞅)"
        r"(?:你|小满|林小满)?(?:本人)?(?:的)?"
        r"(?:照片|自拍|相片|写真)"
    ),
    # Implicit self-view request: "我想看看你", "让我看看小满".
    re.compile(
        r"(?:我(?:想|要|想要)?|想|要|让我|给我|能不能|可以|能否|麻烦)?"
        r"(?:看(?:看|一下|下|一眼)?|瞅瞅)"
        r"(?:你|小满|林小满)(?:本人)?$"
    ),
    # Natural send/request forms: "给我发张自拍", "来一张你的照片".
    re.compile(
        r"(?:我(?:想|要|想要)?|想|要|给我|能不能|可以|能否|麻烦)?"
        r"(?:发|来|给)(?:我)?(?:一|几)?(?:张|个)?"
        r"(?:你|小满|林小满)?(?:的)?(?:照片|自拍|相片|写真)"
    ),
)

_USER_IMAGE_REFERENCE_PATTERNS = (
    re.compile(r"(?:这|那)(?:张|个|幅)?(?:照片|图片|图|相片)"),
    re.compile(r"(?:我|刚才|上面)(?:发|传|贴)(?:的|过的)?(?:照片|图片|图|相片)"),
)


def _normalize_message(message: str) -> str:
    """Drop whitespace and light punctuation without changing event content."""
    return re.sub(r"[\s，。！？、,.!?~～]+", "", message)


def _is_explicit_self_photo_request(message: str) -> bool:
    """Match a narrow set of natural requests for Xiaoman's own photo."""
    normalized = _normalize_message(message)
    if not normalized:
        return False

    if any(pattern.search(normalized) for pattern in _USER_IMAGE_REFERENCE_PATTERNS):
        return False

    return any(pattern.search(normalized) for pattern in _PHOTO_REQUEST_PATTERNS)


def is_directed_look_request(event) -> bool:
    """Return whether a directed message should bypass Airi's generic browse route."""
    if not bool(getattr(event, "is_at_or_wake_command", False)):
        return False

    message = str(getattr(event, "message_str", "") or "").strip()
    if message.startswith("看"):
        return True

    return _is_explicit_self_photo_request(message)


def is_airi_generic_browse_handler(handler) -> bool:
    """Match only the official Airi generic natural-message handler metadata."""
    if getattr(handler, "handler_name", None) != AIRI_GENERIC_HANDLER_NAME:
        return False
    module_path = str(getattr(handler, "handler_module_path", "") or "")
    return AIRI_PLUGIN_SEGMENT in module_path.split(".")


def guard_directed_look_request(event) -> bool:
    """Remove Airi's generic handler in place for this one directed event.

    AstrBot's request stage retains the activated-handler list object while
    iterating it, so slice assignment is required. All inspection failures
    intentionally fail open to preserve the normal chat route.
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
