"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools

from .services.test_bypass import (
    apply_admin_test_bypass,
    inject_test_guidance,
    update_test_mode,
)
from .services.life_broadcast import LifeBroadcastService
from .services.tool_visibility_adapter import hide_gallery_tool_for_xiaoman_request
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Expose Xiaoman's photo tool to the normal LLM conversation path."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._test_mode_umos: set[str] = set()
        self._config = config or {}
        self._life_broadcast = None
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    async def initialize(self) -> None:
        """Start optional schedule broadcast after AstrBot initializes the plugin."""
        if not self._config.get("life_broadcast", {}).get("enable", False):
            return
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None:
            data_dir = StarTools.get_data_dir("astrbot_plugin_xiaoman_personal_interface")
        self._life_broadcast = LifeBroadcastService(self.context, self._config, data_dir)
        self._life_broadcast.start()

    async def terminate(self) -> None:
        if self._life_broadcast is not None:
            await self._life_broadcast.stop()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("xiaoman_broadcast")
    async def xiaoman_broadcast(self, event, action: str = "status"):
        """Admin-only inspection and one-session test for scheduled broadcasts."""
        service = self._life_broadcast
        if service is None:
            yield event.plain_result("林小满日程广播未启用")
            return
        if action == "refresh":
            changed = await service.refresh(force=True)
            yield event.plain_result("日程已刷新" if changed else "日程未变化或当前暂缓生成")
            return
        if action == "test":
            entry = next((e for e in service.state.get("entries", []) if e.get("message") and not e.get("sent") and not e.get("expired")), None)
            if not entry:
                yield event.plain_result("没有已生成的待测广播")
                return
            if service.cfg.get("dry_run", False):
                yield event.plain_result(f"预览：{entry['message']}")
            else:
                from astrbot.api.event import MessageChain
                await self.context.send_message(event.unified_msg_origin, MessageChain().message(entry["message"]))
                yield event.plain_result(f"已向当前会话发送预览（不会标记日程已发送）：{entry['message']}")
            return
        state = service.status()
        next_item = state["next"]
        yield event.plain_result(
            "日程广播状态：enabled={enabled}, dry_run={dry_run}, Life Scheduler={life_scheduler_found}, "
            "hash={hash}, nodes={node_count}, eligible={eligible_count}, sent={sent_count}, targets={target_count}, "
            "next={next}, blocked={blocked_windows}".format(**{**state, "next": next_item})
        )

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
