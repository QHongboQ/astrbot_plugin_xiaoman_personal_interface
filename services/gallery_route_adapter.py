"""Per-event guard for Airi Gallery's generic no-prefix browse handler."""

import re

from astrbot.api import logger


AIRI_PLUGIN_SEGMENT = "astrbot_plugin_airi_gallery"
AIRI_GENERIC_HANDLER_NAME = "handle_gallery_message"

_PUNCTUATION_AND_SPACE = re.compile(r"[\s\W_]+", re.UNICODE)
_VIEW_INTENT = re.compile(r"看看|看一看|看一下|看下|瞅瞅|瞧瞧|看")
_PHOTO_OBJECT = re.compile(r"照片|自拍|相片|写真|图片|图")
_SELF_PRONOUN = re.compile(r"你(?:自己|本人)?")
_SELF_NAME = re.compile(r"林小满|小满")
_REQUEST_OR_SEND = re.compile(r"给我|发|来(?:一张|张)")
_USER_IMAGE_REFERENCE = re.compile(
    r"(?:这|那)张(?:照片|图片|相片|图)"
    r"|(?:这个|那个)(?:照片|图片|相片|图)"
    r"|我发(?:给你)?的(?:照片|图片|相片|图)"
    r"|(?:上面|刚才)的?(?:那张)?(?:照片|图片|相片|图)"
)


def _normalize_message(message: str) -> str:
    """Remove whitespace and punctuation so ordinary Chinese variants compare alike."""
    return _PUNCTUATION_AND_SPACE.sub("", message)


def _is_xiaoman_self_photo_request(message: str) -> bool:
    """Recognize self-photo intent without treating arbitrary image questions as requests."""
    if _USER_IMAGE_REFERENCE.search(message):
        return False

    views = _VIEW_INTENT.search(message)
    photo = _PHOTO_OBJECT.search(message)
    named_target = _SELF_NAME.search(message)
    pronoun_target = _SELF_PRONOUN.search(message)

    # Names are strong only when paired with a view request and a photo, or
    # with the narrow conversational form “让我/想/要看看小满”.
    if views and named_target and photo:
        return True
    if re.search(r"(?:让我|想|要|想要)(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧)(?:林小满|小满)", message):
        return True
    if re.fullmatch(r"(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧)(?:林小满|小满)", message):
        return True

    # A first-person request to see “you” (including “你的照片”) is explicit.
    if views and pronoun_target and re.search(r"(?:看看|看一看|看一下|看下|瞅瞅|瞧瞧|看)你", message):
        return True

    # “发张你的照片” and “给我来张自拍” are explicit send requests.
    if _REQUEST_OR_SEND.search(message) and photo:
        if pronoun_target or named_target or re.search(r"自拍", message):
            return True

    return False


def is_directed_look_request(event) -> bool:
    """Return whether a directly invoked message asks for Xiaoman's own photo."""
    if not bool(getattr(event, "is_at_or_wake_command", False)):
        return False
    message = _normalize_message(str(getattr(event, "message_str", "") or ""))
    return bool(message) and _is_xiaoman_self_photo_request(message)


def is_airi_generic_browse_handler(handler) -> bool:
    """Match only the official Airi generic natural-message handler metadata."""
    if getattr(handler, "handler_name", None) != AIRI_GENERIC_HANDLER_NAME:
        return False
    module_path = str(getattr(handler, "handler_module_path", "") or "")
    return AIRI_PLUGIN_SEGMENT in module_path.split(".")


def guard_directed_look_request(event) -> bool:
    """Remove Airi's generic handler in place for this one directed event.

    AstrBot's request stage retains the activated-handler list object while
    iterating it, so slice assignment is required.  All inspection failures
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
