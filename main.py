"""Plugin entry point for 林小满个人接口."""

from datetime import timedelta

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools

from .services.test_bypass import (
    apply_admin_test_bypass,
    inject_test_guidance,
    update_test_mode,
)
from .services.schedule_broadcast import ScheduleBroadcastService
from .services.fat_fish_bridge import FatFishBridge
from .services.time_awareness_adapter import TimeAwarenessAdapter
from .services.tool_visibility_adapter import hide_gallery_tool_for_xiaoman_request
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Expose Xiaoman's photo tool to the normal LLM conversation path."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._test_mode_umos: set[str] = set()
        self._config = config or {}
        self._schedule_broadcast = None
        self._time_awareness = None
        self._fat_fish_bridge = None
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    async def initialize(self) -> None:
        """Start optional schedule broadcast after AstrBot initializes the plugin."""
        self._time_awareness = TimeAwarenessAdapter(self.context)
        self._fat_fish_bridge = FatFishBridge(self.context, self._time_awareness)
        self._fat_fish_bridge.install()
        cfg = self._config.get("schedule_broadcast", {})
        if not cfg.get("enable", False):
            return
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None:
            data_dir = StarTools.get_data_dir("astrbot_plugin_xiaoman_personal_interface")
        self._schedule_broadcast = ScheduleBroadcastService(
            self.context, self._config, data_dir,
            time_awareness=self._time_awareness, fat_fish=self._fat_fish_bridge)
        self._schedule_broadcast.start()

    async def terminate(self) -> None:
        if self._schedule_broadcast is not None:
            await self._schedule_broadcast.stop()
        elif self._fat_fish_bridge is not None:
            self._fat_fish_bridge.uninstall()

    def get_wallet_policy(self, *, at=None, provider_id=None) -> dict:
        bridge = self._fat_fish_bridge or FatFishBridge(self.context, TimeAwarenessAdapter(self.context))
        return bridge.get_wallet_policy(at=at, provider_id=provider_id)

    async def get_xiaoman_schedule(self, session: str, *, at=None, allow_generate=False):
        adapter = self._time_awareness or TimeAwarenessAdapter(self.context)
        return await adapter.get_daily_schedule(session, at=at, allow_generate=allow_generate)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("xiaoman_broadcast")
    async def xiaoman_broadcast(self, event, action: str = "status", argument: str = ""):
        """Admin-only inspection and management for date-scoped broadcast plans."""
        service = self._schedule_broadcast
        if service is None:
            yield event.plain_result("林小满日程广播未启用")
            return
        now = service._now()
        command_date = now.date() if argument == "today" else now.date() + timedelta(days=1)
        if action == "status":
            state = service.status(now)
            today_snapshot = state["today_snapshot"] or {}
            tomorrow_snapshot = state["tomorrow_snapshot"] or {}
            yield event.plain_result(
                "日程广播：enabled={enabled}, dry_run={dry_run}; TimeAwareness={time_awareness_found}, "
                "Fat Fish={fat_fish_found}, day={day_kind}; "
                "today snapshot={today_id}/{today_count}, plan={today_plan}; "
                "tomorrow snapshot={tomorrow_id}/{tomorrow_count}, plan={tomorrow_plan}; "
                "next={next}, error={last_error}".format(
                    **state,
                    today_id=today_snapshot.get("snapshot_id", "missing"),
                    today_count=len(today_snapshot.get("slots", [])),
                    tomorrow_id=tomorrow_snapshot.get("snapshot_id", "missing"),
                    tomorrow_count=len(tomorrow_snapshot.get("slots", [])),
                    today_plan=state["today_plan_status"],
                    tomorrow_plan=state["tomorrow_plan_status"],
                )
            )
            return
        if action == "refresh":
            changed = await service.refresh(force=True)
            yield event.plain_result("已有快照已刷新" if changed else service.last_error or "没有需要刷新的快照")
            return
        if argument not in {"today", "tomorrow"} and action != "test":
            yield event.plain_result("用法：/xiaoman_broadcast raw|plan|build|reset today|tomorrow")
            return
        if action == "raw":
            snapshot = await service.raw_schedule(command_date)
            if snapshot is None:
                yield event.plain_result("No existing TimeAwareness snapshot.")
                return
            rows = [f"{slot.get('start','?')}-{slot.get('end','?')} {slot.get('name','')} {slot.get('state','')}".strip()
                    for slot in snapshot.get("slots", [])]
            yield event.plain_result(
                f"RAW {snapshot.get('local_date','')} snapshot={snapshot.get('snapshot_id','')} slots={len(rows)}\n"
                + "\n".join(rows)
            )
            return
        if action == "plan":
            plan = service.plan_for_date(command_date)
            if not plan:
                yield event.plain_result("没有已建立的 Xiaoman effective plan。")
                return
            rows = [f"{entry.get('kind')} {entry.get('trigger_at')} {entry.get('name','')} {entry.get('state','')} {entry.get('message','')}"
                    for entry in plan.get("entries", [])]
            yield event.plain_result(
                f"EFFECTIVE {plan.get('local_date')} snapshot={plan.get('snapshot_id')} "
                f"day={plan.get('day_kind')} entries={len(rows)}\n" + "\n".join(rows)
            )
            return
        if action == "build":
            _built, result = await service.build_date(command_date)
            yield event.plain_result(result)
            return
        if action == "reset":
            removed = service.reset_date(command_date)
            yield event.plain_result("已删除 Xiaoman 的该日期计划。" if removed else "该日期没有 Xiaoman 计划。")
            return
        if action == "test":
            success, result = await service.test_entry(argument, event.unified_msg_origin)
            yield event.plain_result(result)
            return
        yield event.plain_result("支持：status、raw、plan、build、reset、test")

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
