"""Plugin entry point for 林小满个人接口."""

from astrbot.api.star import Context, Star

from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Register the single LLM tool exposed by this plugin."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context.add_llm_tools(XiaomanPhotoTool(context))
