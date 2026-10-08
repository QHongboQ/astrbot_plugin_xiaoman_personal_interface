"""Plugin entry point for 林小满个人接口."""

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools

from .services.test_bypass import apply_admin_test_bypass, inject_test_guidance, update_test_mode
from .services.schedule_broadcast import ScheduleBroadcastService
from .services.fat_fish_bridge import FatFishBridge
from .services.time_awareness_adapter import TimeAwarenessAdapter
from .services.tool_visibility_adapter import hide_gallery_tool_for_xiaoman_request
from .tools.photo_tool import XiaomanPhotoTool


class Main(Star):
    """Expose Xiaoman's photo tool and Xiaoman-owned life-day planner."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._test_mode_umos: set[str] = set()
        self._config = config or {}
        self._schedule_broadcast = None
        self._time_awareness = None
        self._fat_fish_bridge = None
        self.context.add_llm_tools(XiaomanPhotoTool(context))

    async def initialize(self) -> None:
        self._time_awareness = TimeAwarenessAdapter(self.context)
        self._fat_fish_bridge = FatFishBridge(self.context, self._time_awareness)
        cfg = self._config.get("schedule_broadcast", {}) or {}
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

    def get_wallet_policy(self, *, at=None, provider_id=None) -> dict:
        bridge = self._fat_fish_bridge or FatFishBridge(self.context, TimeAwarenessAdapter(self.context))
        return bridge.get_wallet_policy(at=at, provider_id=provider_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("xiaoman_broadcast")
    async def xiaoman_broadcast(self, event, action: str = "status", argument: str = ""):
        """Inspect and control Xiaoman-owned rolling life-day plans."""
        service = self._schedule_broadcast
        if service is None:
            yield event.plain_result("林小满生活日规划器未启用")
            return
        now = service._now()
        current_aliases = {"current", "today"}
        next_aliases = {"next", "tomorrow"}
        if action == "status":
            state = await service.status(now)
            fmt = lambda rows: ", ".join(
                f"{row.get('start_at')}→{row.get('end_at')}" for row in rows) or "无"
            next_broadcast = state["next_broadcast"]
            yield event.plain_result(
                f"Planner={state['planner_version']} boundary={state['boundary_clock']}\n"
                f"life_day={state['current_life_day']['start'].isoformat()} → "
                f"{state['current_life_day']['end'].isoformat()}\n"
                f"TimeAwareness={state['time_awareness_available']}\n"
                f"Fat Fish effective peaks={fmt(state['fat_fish_peaks'])}\n"
                f"protected={fmt(state['protected_windows'])}\nfree={fmt(state['free_windows'])}\n"
                f"current_plan={state['current_plan_status']} next_plan={state['next_plan_status']}\n"
                f"next_broadcast={(next_broadcast or {}).get('trigger_at', '无')}\n"
                f"last_planner_error={state['last_planner_error'] or '无'}")
            return
        if action == "regenerate":
            which = "current" if argument in current_aliases else "next" if argument in next_aliases else argument
            if which not in {"current", "next", "cycle"}:
                yield event.plain_result("用法：/xiaoman_broadcast regenerate current|next|cycle")
                return
            results = await service.regenerate(which, now)
            yield event.plain_result("\n".join(
                f"{item.get('status')}: {item.get('reason', item.get('start_at', ''))}" for item in results))
            return
        if action == "refresh":
            changed = await service.refresh()
            yield event.plain_result("广播投递项已从现有计划刷新（未调用 LLM）。" if changed
                                     else "投递项已是最新（未调用 LLM）。")
            return
        if action == "simulate":
            result, error = await service.simulate_time(argument, now)
            if error:
                yield event.plain_result(f"模拟投递未执行：{error}")
                return
            failures = "；".join(f"{item['entry_id']} → {item['umo']}: {item['reason']}"
                                for item in result["failures"]) or "无"
            yield event.plain_result(
                f"模拟投递时间：{result['simulated_at']}\n命中事件：{', '.join(result['hit_event_ids']) or '无'}\n"
                f"目标数：{result['target_count']}；成功数：{result['success_count']}；"
                f"失败数：{result['failure_count']}\n原因：{result.get('reason', '全部成功')}\n失败明细：{failures}")
            return
        if action in {"raw", "plan"}:
            which = "current" if argument in current_aliases or argument == "cycle" else "next"
            plan = service.current_plan(now) if which == "current" else service.next_plan(now)
            if not plan:
                yield event.plain_result(f"没有已建立的 {which} life-day 计划。")
                return
            rows = []
            for row in plan["timeline"]:
                messages = (row.get("broadcast_message", "") if row["kind"] == "NORMAL" else
                            f"进入：{row.get('enter_message', '')}；离开：{row.get('exit_message', '')}")
                rows.append(f"{row['start_at']} → {row['end_at']} {row['kind']} "
                            f"{row['name']} {row.get('state','')} | {messages}")
            yield event.plain_result(
                f"XIAOMAN LIFE DAY {plan['life_day_start']} → {plan['life_day_end']} "
                f"theme={plan['daily_theme']} style={plan['daily_style']} entries={len(rows)}\n" + "\n".join(rows))
            return
        if action == "reset":
            which = "current" if argument in current_aliases else "next" if argument in next_aliases else ""
            if not which:
                yield event.plain_result("用法：/xiaoman_broadcast reset current|next")
                return
            yield event.plain_result("已删除该 life-day 计划。" if service.reset(which, now)
                                     else "该 life day 没有计划。")
            return
        if action == "test":
            success, response = await service.test_entry(argument, event.unified_msg_origin)
            yield event.plain_result(response)
            return
        yield event.plain_result(
            "支持：status、raw|plan current|next|cycle、regenerate current|next|cycle、"
            "refresh、simulate HH:MM、reset current|next、test <entry_id>")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("xiaoman_test")
    async def xiaoman_test(self, event, action: str = "status"):
        yield event.plain_result(update_test_mode(event, self._test_mode_umos, action))

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=100,
    )
    async def bypass_angelheart_for_test_event(self, event) -> None:
        try:
            apply_admin_test_bypass(event, self._test_mode_umos, self.context)
        except Exception:
            logger.warning("Xiaoman test bypass failed open", exc_info=True)

    @filter.on_llm_request()
    async def hide_delegated_gallery_tool(self, event, req) -> None:
        try:
            inject_test_guidance(event, self._test_mode_umos, req)
        except Exception:
            logger.warning("Xiaoman test guidance injection failed open", exc_info=True)
        try:
            hide_gallery_tool_for_xiaoman_request(req)
        except Exception:
            logger.warning("Xiaoman tool visibility adapter failed open", exc_info=True)
