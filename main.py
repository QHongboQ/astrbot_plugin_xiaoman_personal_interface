"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .services.voice_tool_adapter import decorate_tts_speak_tool_args
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Register Xiaoman's stable LLM-facing capabilities."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event, tool, tool_args) -> None:
        """Adapt only a concrete tts_speak invocation; never alter LLM prompts."""
        if getattr(tool, "name", None) != "tts_speak":
            return
        try:
            decorate_tts_speak_tool_args(tool_args)
        except Exception:
            logger.exception("failed to adapt Xiaoman tts_speak arguments")
