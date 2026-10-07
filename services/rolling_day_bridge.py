"""Reversible runtime prompt bridge for Xiaoman's rolling 24-hour life day.

This module does not modify TimeAwareness files or persisted configuration.
It only augments the active TimeAwareness generation prompts at runtime.
"""
from __future__ import annotations

from astrbot.api import logger

from .time_awareness_adapter import TimeAwarenessAdapter


class RollingDayBridge:
    """Treat TimeAwareness generation_time clock as Xiaoman's life-day boundary."""

    def __init__(self, context, adapter: TimeAwarenessAdapter | None = None, fat_fish=None):
        self.context = context
        self.adapter = adapter or TimeAwarenessAdapter(context)
        self.fat_fish = fat_fish
        self._generation = None
        self._original_plan_prompt = None
        self._original_boundary_prompt = None
        self._plan_wrapper = None
        self._boundary_wrapper = None

    def _instruction(self) -> str:
        boundary = self.adapter.get_generation_boundary()
        if not boundary.get("available"):
            return ""
        clock = boundary["clock"]
        raw = boundary["raw"]
        return (
            "\n\n<XIAOMAN_ROLLING_DAY>\n"
            f"TimeAwareness ai_daily.generation_time={raw}; "
            f"将每天 {clock} 视为林小满的生活日边界。\n"
            f"规划意义上的一个生活日是当日 {clock} 到次日 {clock} 的连续24小时。"
            "官方输出协议仍必须严格保持自然日00:00-24:00、不得跨午夜；这里改变的是规划语义，不改变输出格式。\n"
            f"目标自然日的00:00-{clock}属于上一生活日的深夜延续；{clock}-24:00属于新的生活日主体。"
            "不要因为24:00是JSON日程的存储边界，就机械地在23点左右安排洗漱、刷手机或入睡。"
            "如果当晚的娱乐、社交、创作或外出活动自然会持续到午夜，应让本日活动合理延续至24:00，"
            f"并让次日00:00-{clock}结合最近日程与连续性自然承接回程、宵夜、聊天、游戏、创作或之后的入睡。"
            "睡觉时间应由当天体力、次日安排、最近睡眠债和真实活动决定，而不是由自然日边界决定。"
            f"如果上一自然日23点后仍明显活跃，生成当前自然日00:00-{clock}时优先保持连续，"
            "不要在00:00无理由重置成已经睡着。"
            "\n</XIAOMAN_ROLLING_DAY>"
        )

    def peak_windows(self) -> list[tuple[str, str]]:
        """Read current Fat Fish periods without caching or changing its config."""
        fish = self.fat_fish.discover() if self.fat_fish is not None else None
        if fish is None:
            return []
        try:
            periods = fish._periods()
        except Exception:
            return []
        result = []
        for period in periods or []:
            try:
                start, end = int(period.start), int(period.end)
            except (AttributeError, TypeError, ValueError):
                continue
            if start < 0 or end <= start:
                continue
            result.append((f"{start // 3600:02d}:{(start % 3600) // 60:02d}",
                           f"{end // 3600:02d}:{(end % 3600) // 60:02d}"))
        return result

    def _peak_instruction(self, now) -> str:
        if now is None or self.adapter.get_day_policy(now).get("kind") not in {"workday", "adjusted"}:
            return ""
        windows = self.peak_windows()
        if not windows:
            return ""
        ranges = "、".join(f"{start}-{end}" for start, end in windows)
        return (
            "\n\n<XIAOMAN_FAT_FISH_PEAK_BLOCKS>\n"
            f"Fat Fish 当前工作日高峰窗口：{ranges}。本次目标日为工作日或调休工作日；"
            "规划时每个窗口优先由一个连贯的主要活动时段覆盖，可自然略早开始或略晚结束。"
            "咖啡、饮料、短暂吃东西/休息、聊天、刷手机、逛小店、拍照、如厕等同一情境内的小事"
            "写进该时段 state，不拆成独立活动。只有上课转跨城、学校转医院等重大情境变化才拆分。"
            "不得为了填满窗口编造工作或学习；可使用有日程依据的课程、创作、外出、办事、社交、休息等。"
            "周末和节假日不追加此约束。\n</XIAOMAN_FAT_FISH_PEAK_BLOCKS>"
        )

    def install(self) -> bool:
        plugin = self.adapter.discover()
        service = getattr(plugin, "daily_schedule_service", None) if plugin else None
        generation = getattr(service, "generation", None) if service else None
        if generation is None:
            return False

        if self._generation is generation and self._plan_wrapper is not None:
            if getattr(generation, "_build_prompt", None) is self._plan_wrapper:
                return True

        # If TimeAwareness was reloaded, restore our old instance only when it
        # still points to our wrappers, then attach to the new generation object.
        self.uninstall()
        self._generation = generation

        original_plan = getattr(generation, "_build_prompt", None)
        if not callable(original_plan):
            self._generation = None
            return False

        bridge = self

        def plan_wrapper(*args, **kwargs):
            base = original_plan(*args, **kwargs)
            return str(base) + bridge._instruction() + bridge._peak_instruction(kwargs.get("now"))

        self._original_plan_prompt = original_plan
        self._plan_wrapper = plan_wrapper
        generation._build_prompt = plan_wrapper

        original_boundary = getattr(generation, "_build_boundary_prompt", None)
        if callable(original_boundary):
            def boundary_wrapper(*args, **kwargs):
                base = original_boundary(*args, **kwargs)
                return str(base) + bridge._instruction() + bridge._peak_instruction(kwargs.get("now"))

            self._original_boundary_prompt = original_boundary
            self._boundary_wrapper = boundary_wrapper
            generation._build_boundary_prompt = boundary_wrapper

        return True

    def ensure_installed(self) -> bool:
        """Re-attach after a TimeAwareness reload without touching its files."""
        plugin = self.adapter.discover()
        service = getattr(plugin, "daily_schedule_service", None) if plugin else None
        generation = getattr(service, "generation", None) if service else None
        if generation is self._generation and (
            self._plan_wrapper is not None
            and getattr(generation, "_build_prompt", None) is self._plan_wrapper
        ):
            return True
        return self.install()

    def status(self) -> dict:
        boundary = self.adapter.get_generation_boundary()
        return {
            **boundary,
            "installed": bool(
                self._generation is not None
                and self._plan_wrapper is not None
                and getattr(self._generation, "_build_prompt", None) is self._plan_wrapper
            ),
            "peak_windows": self.peak_windows(),
        }

    def uninstall(self) -> None:
        generation = self._generation
        if generation is not None:
            try:
                if (
                    self._plan_wrapper is not None
                    and getattr(generation, "_build_prompt", None) is self._plan_wrapper
                ):
                    generation._build_prompt = self._original_plan_prompt
                if (
                    self._boundary_wrapper is not None
                    and getattr(generation, "_build_boundary_prompt", None)
                    is self._boundary_wrapper
                ):
                    generation._build_boundary_prompt = self._original_boundary_prompt
            except Exception:
                logger.warning("Xiaoman rolling-day bridge restore failed", exc_info=True)
        self._generation = None
        self._original_plan_prompt = None
        self._original_boundary_prompt = None
        self._plan_wrapper = None
        self._boundary_wrapper = None
