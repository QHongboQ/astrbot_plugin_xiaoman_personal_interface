"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .services.gallery_route_adapter import guard_directed_look_request
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Expose the photo tool and protect Xiaoman-directed conversations."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def guard_directed_gallery_route(self, event) -> None:
        """Keep explicitly directed ``看...`` messages on the normal LLM route."""
        try:
            guard_directed_look_request(event)
        except Exception:
            # The guard must never block chat if an event implementation changes.
            logger.warning("Xiaoman gallery route guard failed open", exc_info=True)
