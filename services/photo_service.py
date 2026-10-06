"""Delegation service for the existing photo-sending tool."""

import re

from astrbot.api import logger


_SUCCESS_RESULT = re.compile(
    r"^\s*已从\s*林小满\s*分类发送\s*1\s*张图片\s*[。.!！]?\s*$"
)


def _confirms_requested_photo_send(result) -> bool:
    """Accept only Airi's explicit confirmation for the requested category/count."""
    return isinstance(result, str) and _SUCCESS_RESULT.fullmatch(result) is not None


class PhotoService:
    """Find and invoke the pre-registered photo sender without reimplementing it."""

    def __init__(self, plugin_context) -> None:
        self._plugin_context = plugin_context

    async def send(self, current_context) -> str:
        """Send one 林小满 photo through the registered gallery tool."""
        tool_manager = self._plugin_context.get_llm_tool_manager()
        gallery_tool = tool_manager.get_func("gallery_send")
        if gallery_tool is None:
            return "PHOTO_SEND_FAILED"

        try:
            delegated_result = await gallery_tool.call(
                current_context,
                category="林小满",
                count=1,
            )
        except Exception:
            logger.exception("send_xiaoman_photo delegation failed")
            return "PHOTO_SEND_FAILED"

        if _confirms_requested_photo_send(delegated_result):
            return "PHOTO_SENT"
        return "PHOTO_SEND_FAILED"
