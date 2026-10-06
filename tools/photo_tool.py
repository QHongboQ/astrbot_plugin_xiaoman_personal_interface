"""The LLM-facing tool for sending a 林小满 photo."""

from astrbot.core.agent.tool import FunctionTool

from ..services.photo_service import PhotoService


class XiaomanPhotoTool(FunctionTool):
    """Delegate the approved photo send to the registered photo sender."""

    def __init__(self, plugin_context) -> None:
        super().__init__(
            name="send_xiaoman_photo",
            description=(
                "向当前聊天对象发送林小满本人/自拍的一张真实照片。"
                "当用户请求林小满的照片或自拍时，若你根据人格和上下文决定愿意发送，调用此工具；"
                "若决定不发送，则正常回复而不要调用。是否调用由你根据当前人格和上下文决定。"
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
