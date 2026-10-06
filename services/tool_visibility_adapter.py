"""Per-request visibility adapter for the delegated photo tool."""

from astrbot.api import logger


def hide_gallery_tool_for_xiaoman_request(req) -> bool:
    """Hide ``gallery_send`` only when Xiaoman's public wrapper is available.

    This mutates only the current request's ToolSet. The global tool manager is
    untouched, so the wrapper can still resolve and call ``gallery_send``.
    Unknown or incompatible request objects fail open.
    """
    tool_set = getattr(req, "func_tool", None)
    names_method = getattr(tool_set, "names", None)
    remove_method = getattr(tool_set, "remove_tool", None)
    if not callable(names_method) or not callable(remove_method):
        logger.warning("Xiaoman tool visibility adapter: request ToolSet unavailable; failing open")
        return False

    try:
        names = names_method()
    except Exception:
        logger.warning("Xiaoman tool visibility adapter: unable to inspect request tools; failing open")
        return False

    if not isinstance(names, (list, tuple, set)):
        logger.warning("Xiaoman tool visibility adapter: incompatible tool-name list; failing open")
        return False

    if "send_xiaoman_photo" not in names:
        logger.warning(
            "Xiaoman photo tool is not exposed on this LLM request; leaving available tools unchanged"
        )
        return False

    if "gallery_send" not in names:
        return False

    try:
        remove_method("gallery_send")
    except Exception:
        logger.warning("Xiaoman tool visibility adapter: unable to hide gallery_send; failing open")
        return False
    return True
