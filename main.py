"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .services.test_bypass import (
    apply_admin_test_bypass,
    inject_test_guidance,
    update_test_mode,
)
from .services.tool_visibility_adapter import hide_gallery_tool_for_xiaoman_request
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Expose Xiaoman's photo tool to the normal LLM conversation path."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._test_mode_umos: set[str] = set()
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("xiaoman_test")
    async def xiaoman_test(self, event, action: str = "status"):
        """Toggle the authorized administrator's test mode for the current UMO."""
        response = update_test_mode(event, self._test_mode_umos, action)
        yield event.plain_result(response)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=100,
    )
    async def bypass_angelheart_for_test_event(self, event) -> None:
        """Isolate one enabled administrator test event from AngelHeart."""
        try:
            apply_admin_test_bypass(event, self._test_mode_umos, self.context)
        except Exception:
            logger.warning("Xiaoman test bypass failed open", exc_info=True)

    @filter.on_llm_request()
    async def hide_delegated_gallery_tool(self, event, req) -> None:
        """Expose only the Xiaoman wrapper for the current model request."""
        try:
            inject_test_guidance(event, self._test_mode_umos, req)
        except Exception:
            logger.warning("Xiaoman test guidance injection failed open", exc_info=True)

        try:
            hide_gallery_tool_for_xiaoman_request(req)
        except Exception:
            # Visibility changes are request-local and must fail open.
            logger.warning("Xiaoman tool visibility adapter failed open", exc_info=True)
