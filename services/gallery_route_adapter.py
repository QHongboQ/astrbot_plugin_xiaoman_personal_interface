"""Per-event guard for Airi Gallery's generic no-prefix browse handler."""

from astrbot.api import logger


AIRI_PLUGIN_SEGMENT = "astrbot_plugin_airi_gallery"
AIRI_GENERIC_HANDLER_NAME = "handle_gallery_message"


def is_directed_look_request(event) -> bool:
    """Return whether this is a directly invoked message beginning with ``看``."""
    if not bool(getattr(event, "is_at_or_wake_command", False)):
        return False
    return str(getattr(event, "message_str", "") or "").strip().startswith("看")


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
