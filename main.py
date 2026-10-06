"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .services.voice_guidance import append_voice_guidance, is_tts_speak_available
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Register Xiaoman's stable LLM-facing capabilities."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    @filter.on_llm_request()
    async def on_llm_request(self, event, request) -> None:
        """Append voice guidance only while the public tts_speak tool is available."""
        try:
            if not is_tts_speak_available(self.context):
                return
            request.system_prompt = append_voice_guidance(
                getattr(request, "system_prompt", "")
            )
        except Exception:
            logger.exception("failed to inject Xiaoman voice guidance")
