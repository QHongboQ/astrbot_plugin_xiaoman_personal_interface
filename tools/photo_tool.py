"""The LLM-facing tool for sending a 林小满 photo."""

from astrbot.core.agent.tool import FunctionTool

from services.photo_service import PhotoService


class XiaomanPhotoTool(FunctionTool):
    """Delegate the approved photo send to the registered photo sender."""

    def __init__(self, plugin_context) -> None:
        super().__init__(
            name="send_xiaoman_photo",
            description=(
                "向当前聊天对象发送林小满的一张照片。"
                "仅在上层人格和上下文已经决定发送时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        self._photo_service = PhotoService(plugin_context)

    async def call(self, context, **kwargs) -> str:
        """Use the same FunctionTool context supplied for this invocation."""
        return await self._photo_service.send(context)
