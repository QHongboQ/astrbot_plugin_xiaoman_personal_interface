"""Per-event routing between Xiaoman's visual requests and Airi's gallery browse handler."""

import re

from astrbot.api import logger


AIRI_PLUGIN_SEGMENT = "astrbot_plugin_airi_gallery"
AIRI_GENERIC_HANDLER_NAME = "handle_gallery_message"

_NORMALIZE = re.compile(r"[\s\W_]+", re.UNICODE)
_VIEW_ACTION = re.compile(r"看一看|看一下|看一眼|看看|看下|瞅一眼|瞅瞅|瞧一眼|瞧瞧|看")
_ACTION = re.compile(
    r"我想要|我想|我要|想要|想看|想|要看|能不能|可不可以|可以|请"
    r"|给我|给|发给我|发我|发|传给我|传我|传|送给我|送我|送|分享|展示"
    r"|来|整|弄|找|有没(?:有)?|有没有|有|"
    r"看一看|看一下|看一眼|看看|看下|瞅一眼|瞅瞅|瞧一眼|瞧瞧|看"
)
_MEDIA_OBJECT = re.compile(r"截图|照片|自拍|相片|写真|图片|图像|影像|美照|图|文件")
_APPEARANCE_OBJECT = re.compile(
    r"长什么样|长啥样|什么样子|啥样|长相|模样|外貌|长得"
)
_VISUAL_OBJECT = re.compile(
    rf"(?:{_MEDIA_OBJECT.pattern})|(?:{_APPEARANCE_OBJECT.pattern})"
)
_SELF_OWNER = re.compile(r"^(?:你本人|你自己|你的|你|林小满|小满|自己)(?:的)?")
_USER_OWNER = re.compile(r"^(?:我的|我自己|我本人|我)(?:的)?")
_SELF_VIEW_TARGET = re.compile(
    r"(?:看看|看一看|看一下|看一眼|看下|瞅一眼|瞅瞅|瞧一眼|瞧瞧|看)"
    r"(?:你本人|你自己|你|林小满|小满)(?:吗|呢|呀)?$"
)
_MEASURE_PREFIX = re.compile(r"^(?:[一二两三四五六七八九十几\d]+)?(?:张|个|幅|套|份|点|些)")
_ACTION_FILLER = re.compile(r"^(?:一下|一眼|一张|张|个)")
_VISUAL_QUALIFIER = re.compile(
    r"(?:好看|漂亮|可爱|帅气|清晰|高清|精美|精致|唯美|复古|经典|新|旧|美)(?:的)?$"
)
_USER_SOURCE = re.compile(r"(?<!给)我(?:刚(?:才)?|最近)?(?:发(?:给你)?|上传|传|拍)(?:的|给你的)")
_DEICTIC_REFERENCE = re.compile(r"(?:上面|刚才|这(?:张|个|幅)|那(?:张|个|幅))")
_IMAGE_ANALYSIS = re.compile(r"分析|识别|解释|总结|提取|OCR", re.IGNORECASE)
_FOLLOW_UP = re.compile(r"(?:照片|自拍|相片|写真|图片|图像|美照)(?:呢|呀|吗|可以吗|行吗)$")


def _normalize_message(message: str) -> str:
    """Normalize punctuation and whitespace while preserving Chinese/digit tokens."""
    return _NORMALIZE.sub("", message)


def _modifier_before_visual_object(message: str, visual_match: re.Match) -> str:
    """Return the object modifier after the final request/view action, if any."""
    prefix = message[: visual_match.start()]
    qualifier = _VISUAL_QUALIFIER.search(prefix)
    if qualifier and qualifier.end() == len(prefix):
        prefix = prefix[: qualifier.start()]

    # A leading second-person pronoun is an actor when it directly precedes a
    # view verb ("你看看照片"), not a photo possessor.
    actor_view = re.match(
        r"^(?:你|小满|林小满)(?:自己)?(?=" + _VIEW_ACTION.pattern + r")",
        prefix,
    )
    if actor_view:
        prefix = prefix[actor_view.end() :]

    actions = list(_ACTION.finditer(prefix))
    if actions:
        prefix = prefix[actions[-1].end() :]

    prefix = _MEASURE_PREFIX.sub("", prefix)
    prefix = _ACTION_FILLER.sub("", prefix)
    return prefix


def _resolve_visual_target(message: str, visual_match: re.Match | None) -> str:
    """Resolve explicit self/other target before allowing a directed implicit target."""
    if visual_match is None:
        return "self" if _SELF_VIEW_TARGET.search(message) else "implicit"

    modifier = _modifier_before_visual_object(message, visual_match)
    # A self-reference after a conversational lead-in is explicit even when
    # it is not the first token: "能不能告诉我你长什么样".
    if _APPEARANCE_OBJECT.search(message) and re.search(
        r"(?:你本人|你自己|你的|你|林小满|小满)(?:的)?$", modifier
    ):
        return "self"
    if not modifier:
        return "implicit"

    user_owner = _USER_OWNER.match(modifier)
    if user_owner:
        # The user is the possessor/source of the requested visual object.
        # A recipient is removed with the preceding verb phrase, leaving the
        # requested target (e.g. "给我发张你的照片" -> "你的").
        return "competing"

    self_owner = _SELF_OWNER.match(modifier)
    if self_owner:
        remaining = modifier[self_owner.end() :]
        # Possession scopes over the entire noun phrase. Any lexical modifier
        # between Xiaoman's possessor and the media head names another target,
        # regardless of whether Chinese repeats 的.
        if remaining:
            return "competing"
        return "self"

    # A noun phrase attached to the visual object supplies its own target.
    # This is open-class, so an unfamiliar noun does not need a code change.
    if _VISUAL_QUALIFIER.fullmatch(modifier):
        return "implicit"
    return "competing"


def _is_xiaoman_visual_request(message: str, *, directed: bool) -> bool:
    """Classify using directedness, target, action, object, exclusions, and precedence."""
    if not directed or not message:
        return False
    if message.startswith("/"):
        return False

    visual_match = _VISUAL_OBJECT.search(message)

    # Appearance questions can name Xiaoman without a media noun ("看看小满",
    # "你长什么样"). They are still visual requests, while a bare category
    # command such as "看看默认" has neither a self target nor appearance intent.
    if visual_match is None:
        return bool(_SELF_VIEW_TARGET.search(message) or _APPEARANCE_OBJECT.search(message))

    prefix = message[: visual_match.start()]
    if _USER_SOURCE.search(prefix) or _DEICTIC_REFERENCE.search(message[: visual_match.end()]):
        return False
    if _IMAGE_ANALYSIS.search(message) and _MEDIA_OBJECT.search(message):
        return False

    # "帮我看看图片" is an inspection request with an omitted object owner,
    # not a request for the addressed speaker's own photo.
    if re.search(r"帮我", prefix) and _VIEW_ACTION.search(prefix):
        return False

    # Actor role is resolved before implicit-subject inheritance. In a
    # directed chat, "你看看照片" asks Xiaoman to inspect an unspecified
    # picture; it does not ask for her own picture.
    if re.match(
        r"^(?:你|小满|林小满)(?:自己)?(?=" + _VIEW_ACTION.pattern + r")",
        prefix,
    ):
        return False

    target = _resolve_visual_target(message, visual_match)
    has_prefix_action = bool(_ACTION.search(prefix))
    suffix = message[visual_match.end() :]
    has_suffix_action = bool(
        re.match(r"^(?:发|传|送|分享|展示|给|来|整|弄|找)(?:给我|我)?(?:一下|一张|张|个)?", suffix)
    )
    has_view_action = bool(_VIEW_ACTION.search(prefix))
    has_action = has_prefix_action or has_suffix_action or has_view_action
    if target == "self":
        return bool(
            has_action
            or _FOLLOW_UP.search(message)
            or _APPEARANCE_OBJECT.search(message)
        )
    if target == "competing":
        return False

    # Bare Airi category/range/default requests have no visual-request object;
    # explicit category words preceding a photo object resolve as competing targets.
    if _MEDIA_OBJECT.fullmatch(visual_match.group(0)) and not (
        has_action
        or re.search(r"(?:照片|自拍|相片|写真|图片|图像|美照)有(?:吗|没有)", message)
        or _FOLLOW_UP.search(message)
    ):
        return False

    return bool(
        has_action
        or re.search(r"(?:有没(?:有)?|有没有)\s*(?:照片|自拍|相片|写真|图片|图像|美照)", message)
        or re.search(r"(?:照片|自拍|相片|写真|图片|图像|美照)有(?:吗|没有)", message)
        or _VIEW_ACTION.search(message)
        or _FOLLOW_UP.search(message)
        or _APPEARANCE_OBJECT.search(message)
    )


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
