from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_photo_tool import MAIN_MODULE
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.fat_fish_bridge import FatFishBridge
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.schedule_broadcast import ScheduleBroadcastService
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.time_awareness_adapter import TimeAwarenessAdapter

TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
ROOT = Path(__file__).parents[1]
FAT_FISH_MODULE = sys.modules[FatFishBridge.__module__]


class FakeDay:
    def __init__(self, boundary="04:00", *, kind="workday", available=True):
        self.boundary = boundary
        self.kind = kind
        self.available = available
        self.now = datetime(2026, 10, 9, 4, 0, tzinfo=TZ)
        self.context = {"available": True, "worldview": "用户给定世界观", "use_persona": True,
                        "theme_pool": ["夜游"], "style_pool": ["随性"],
                        "allow_custom_theme": True,
                        "adaptive": {"recent_days": 5, "state_continuity_enabled": True},
                        "calendar_days": [], "weather": [{"date": "2026-10-09", "forecast": "晴"}]}

    def current_time(self):
        return self.now

    def discover(self):
        return object() if self.available else None

    def get_generation_boundary(self):
        hour, minute = map(int, self.boundary.split(":"))
        return {"available": self.available, "clock": self.boundary, "hour": hour,
                "minute": minute, "target_day_offset": 1, "raw": "-" + self.boundary}

    def life_day_window(self, at=None):
        now = at or self.current_time()
        hour, minute = map(int, self.boundary.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        return {**self.get_generation_boundary(), "start": start,
                "end": start + timedelta(days=1), "timezone": "Asia/Shanghai"}

    def get_day_policy(self, at=None):
        return {"available": self.available, "kind": self.kind if self.available else "unknown",
                "label": "calendar label", "evaluated_at": at}

    async def planner_context(self, start_at, end_at):
        day = start_at.date()
        rows = []
        while day < end_at.date() or day == (end_at - timedelta(microseconds=1)).date():
            rows.append({"date": day.isoformat(), "kind": self.kind if self.available else "unknown",
                         "label": "calendar label"})
            day += timedelta(days=1)
        return {**self.context, "calendar_days": rows}


class FakeFish:
    def __init__(self, windows=None):
        self.windows = windows or []
        self.calls = []
        self.policy = {"found": True, "enabled": True, "allowed": True, "state": "offpeak",
                       "manual_override": "auto", "provider_affected": True,
                       "timezone": "Asia/Shanghai", "admins_bypass": True}

    def effective_peak_windows(self, start, end, provider, calendar_days):
        self.calls.append((start, end, provider, calendar_days))
        if not any(row.get("kind") in {"workday", "adjusted", "adjusted_workday"}
                   for row in calendar_days):
            return []
        return [row for row in self.windows if row["start_at"] < end and row["end_at"] > start]

    def get_wallet_policy(self, *, at=None, provider_id=None):
        return dict(self.policy)


class Context:
    def __init__(self, rows=None, *, output_factory=None):
        self.rows = list(rows or [types.SimpleNamespace(user_id="qq:FriendMessage:one", platform_id="qq")])
        self.output_factory = output_factory
        self.raw_output = None
        self.provider_type = "llm"
        self.llm_calls = []
        self.sent = []
        self.fail = set()
        self.false = set()
        self.persona_manager = types.SimpleNamespace(get_default_persona_v3=self.persona)
        self.conversation_manager = types.SimpleNamespace(get_conversations=self.conversations)
        self.provider_calls = []
        self.stars = []

    async def conversations(self):
        return self.rows

    async def get_current_chat_provider_id(self, umo):
        return "planner-provider"

    def get_provider_by_id(self, provider_id):
        if provider_id != "planner-provider":
            return None
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(
            id=provider_id, model="deepseek-chat", type=self.provider_type))

    async def persona(self):
        return {"prompt": "default persona prompt"}

    def get_all_stars(self):
        return self.stars

    def get_platform_inst(self, platform_id):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="aiocqhttp"))

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt=None, tools=None, **kwargs):
        self.llm_calls.append({"provider": chat_provider_id, "prompt": prompt,
                               "system_prompt": system_prompt, "tools": tools, **kwargs})
        if self.raw_output is not None:
            return types.SimpleNamespace(completion_text=self.raw_output)
        if self.output_factory:
            return types.SimpleNamespace(completion_text=json.dumps(
                self.output_factory(prompt), ensure_ascii=False))
        return types.SimpleNamespace(completion_text="{}")

    async def send_message(self, umo, chain):
        if umo in self.fail:
            raise RuntimeError("offline")
        if umo in self.false:
            return False
        self.sent.append((umo, chain))
        return True


class TestMessageChain:
    def __init__(self):
        self.text = ""
    def message(self, text):
        self.text = text
        return self


def window(start, end):
    return {"start_at": datetime.fromisoformat(start).astimezone(TZ),
            "end_at": datetime.fromisoformat(end).astimezone(TZ),
            "source_peak_start": datetime.fromisoformat(start).astimezone(TZ) + timedelta(minutes=5),
            "source_peak_end": datetime.fromisoformat(end).astimezone(TZ) - timedelta(minutes=5)}


def valid_response_for(planner_input):
    rows = []
    for item in planner_input["free_windows"]:
        rows.append({"id": "N-" + item["id"], "kind": "NORMAL", "category": "rest", "start_at": item["start_at"],
                     "end_at": item["end_at"], "name": "日常安排", "state": "自然活动",
                     "broadcast_message": "今天按自己的节奏安排生活。"})
    for item in planner_input["protected_windows"]:
        rows.append({"id": "B-" + item["id"], "kind": "BRIDGE", "category": "social", "start_at": item["start_at"],
                     "end_at": item["end_at"], "name": "连续活动", "state": "内部阶段依次推进",
                     "source_peak_start": item["source_peak_start"],
                     "source_peak_end": item["source_peak_end"],
                     "enter_message": "我先去参加今天的活动啦。", "exit_message": "活动告一段落，接着安排下一件事。"})
    rows.sort(key=lambda row: row["start_at"])
    return {"daily_theme": "夜色灵感", "daily_style": "轻松随性", "timeline": rows}


class LifeDayPlannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        event_module = sys.modules.get("astrbot.api.event")
        if event_module is not None and not hasattr(event_module, "MessageChain"):
            event_module.MessageChain = TestMessageChain
        self.tmp = tempfile.TemporaryDirectory()
        self.start = datetime(2026, 10, 9, 4, 0, tzinfo=TZ)
        self.day = FakeDay()
        self.day.now = self.start
        self.day.context["calendar_days"] = [{"date": "2026-10-09", "kind": "workday", "label": ""},
                                             {"date": "2026-10-10", "kind": "weekend", "label": ""}]
        self.context = Context()
        self.fish = FakeFish()
        self.service = ScheduleBroadcastService(
            self.context, {"schedule_broadcast": {"enable": True, "provider_id": "planner-provider"}},
            self.tmp.name, time_awareness=self.day, fat_fish=self.fish)

    def tearDown(self):
        self.tmp.cleanup()

    async def _input(self, protected=None):
        if protected is not None:
            self.fish.windows = protected
        return await self.service.planner_input(self.start, "planner-provider")

    def _use_config_only_fatfish(self, **overrides):
        config = {"enabled": True, "timezone": "Asia/Shanghai",
                  "peak_periods": "09:00-12:00,14:00-18:00",
                  "peak_weekdays": "0,1,2,3,4,5,6",
                  "affected_providers": "deepseek", "gate_when_provider_unknown": True,
                  "manual_override": "auto", "admins_bypass": True}
        config.update(overrides)
        fish = types.SimpleNamespace(config=config)
        self.context.stars = [types.SimpleNamespace(
            name="astrbot_plugin_fat_fish_wallet", activated=True, star_cls=fish)]
        self.service.fat_fish = FatFishBridge(self.context, self.day)
        return fish

    async def test_exact_guarded_protected_and_free_windows_are_sent_to_planner(self):
        self.fish.windows = [
            window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00"),
            window("2026-10-09T14:00:00+08:00", "2026-10-09T18:00:00+08:00"),
        ]
        planner_input = await self.service.planner_input(self.start, "planner-provider")
        self.assertEqual([(x["start_at"], x["end_at"]) for x in planner_input["protected_windows"]], [
            ("2026-10-09T08:55:00+08:00", "2026-10-09T12:05:00+08:00"),
            ("2026-10-09T13:55:00+08:00", "2026-10-09T18:05:00+08:00"),
        ])
        self.assertEqual([(x["start_at"], x["end_at"]) for x in planner_input["free_windows"]], [
            ("2026-10-09T04:00:00+08:00", "2026-10-09T08:55:00+08:00"),
            ("2026-10-09T12:05:00+08:00", "2026-10-09T13:55:00+08:00"),
            ("2026-10-09T18:05:00+08:00", "2026-10-10T04:00:00+08:00"),
        ])
        self.assertNotIn(("2026-10-09T12:05:00+08:00", "2026-10-09T14:05:00+08:00"),
                         [(x["start_at"], x["end_at"]) for x in planner_input["free_windows"]])
        self.assertIn("protected_windows", planner_input)
        self.assertIn("free_windows", planner_input)
        embedded = json.loads(self.service._planner_prompt(planner_input).split("PLANNER_INPUT:\n", 1)[1])
        self.assertEqual(embedded["protected_windows"], planner_input["protected_windows"])
        self.assertEqual(embedded["free_windows"], planner_input["free_windows"])

    async def test_real_fatfish_111_config_produces_guarded_planner_windows(self):
        self._use_config_only_fatfish()
        planner_input = await self.service.planner_input(self.start, "planner-provider")
        self.assertEqual([(row["start_at"], row["end_at"])
                          for row in planner_input["protected_windows"]], [
            ("2026-10-09T08:55:00+08:00", "2026-10-09T12:05:00+08:00"),
            ("2026-10-09T13:55:00+08:00", "2026-10-09T18:05:00+08:00"),
        ])
        self.assertEqual([(row["start_at"], row["end_at"])
                          for row in planner_input["free_windows"]], [
            ("2026-10-09T04:00:00+08:00", "2026-10-09T08:55:00+08:00"),
            ("2026-10-09T12:05:00+08:00", "2026-10-09T13:55:00+08:00"),
            ("2026-10-09T18:05:00+08:00", "2026-10-10T04:00:00+08:00"),
        ])

    async def test_full_24h_continuous_timeline_is_accepted(self):
        planner_input = await self._input()
        valid, reason = self.service.validate_timeline(planner_input, valid_response_for(planner_input)["timeline"])
        self.assertTrue(valid, reason)

    async def test_gap_is_rejected(self):
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        timeline[0]["end_at"] = (self.start + timedelta(hours=2)).isoformat()
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_overlap_is_rejected(self):
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        timeline[0]["end_at"] = (self.start + timedelta(hours=3)).isoformat()
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_normal_crossing_protected_window_is_rejected(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")]
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        timeline[0]["end_at"] = data["protected_windows"][0]["end_at"]
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_protected_window_with_multiple_bridges_is_rejected(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")]
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        bridge = next(row for row in timeline if row["kind"] == "BRIDGE")
        duplicate = dict(bridge, id="duplicate-bridge")
        timeline.append(duplicate)
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_missing_bridge_is_rejected(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")]
        data = await self._input()
        timeline = [row for row in valid_response_for(data)["timeline"] if row["kind"] != "BRIDGE"]
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_bridge_boundaries_must_match_exactly(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")]
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        bridge = next(row for row in timeline if row["kind"] == "BRIDGE")
        bridge["start_at"] = (datetime.fromisoformat(bridge["start_at"]) + timedelta(minutes=1)).isoformat()
        self.assertFalse(self.service.validate_timeline(data, timeline)[0])

    async def test_cross_midnight_free_window_is_preserved(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00"),
                             window("2026-10-09T14:00:00+08:00", "2026-10-09T18:00:00+08:00")]
        data = await self._input()
        self.assertEqual(data["free_windows"][-1]["start_at"], "2026-10-09T18:05:00+08:00")
        self.assertEqual(data["free_windows"][-1]["end_at"], "2026-10-10T04:00:00+08:00")

    async def test_no_peaks_weekend_or_holiday_yields_one_free_window(self):
        data = await self._input()
        self.assertEqual(len(data["free_windows"]), 1)
        self.assertEqual(data["free_windows"][0]["start_at"], data["life_day"]["start_at"])
        self.assertEqual(data["free_windows"][0]["end_at"], data["life_day"]["end_at"])
        valid, reason = self.service.validate_timeline(data, valid_response_for(data)["timeline"])
        self.assertTrue(valid, reason)
        self.day.kind = "weekend"
        data = await self._input([window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")])
        self.assertEqual(data["protected_windows"], [])
        self.assertEqual(len(data["free_windows"]), 1)
        self.day.kind = "holiday"
        data = await self._input([window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")])
        self.assertEqual(data["protected_windows"], [])

    async def test_planner_context_has_worldview_theme_style_weather(self):
        data = await self._input()
        self.assertEqual(data["worldview"], "用户给定世界观")
        self.assertEqual(data["theme_pool"], ["夜游"])
        self.assertEqual(data["style_pool"], ["随性"])
        self.assertTrue(data["weather"])

    async def test_activity_pool_parser_supports_weighted_and_unweighted_items(self):
        self.service.cfg["activity_pool"] = ["密室逃脱,7", "KTV,6", "游乐园", "太重,999",
                                               "坏权重,abc", "海边,夜游,4"]
        self.assertEqual(self.service._activity_pool(), [
            {"name": "密室逃脱", "weight": 7}, {"name": "KTV", "weight": 6},
            {"name": "游乐园", "weight": 1}, {"name": "太重", "weight": 10},
            {"name": "坏权重", "weight": 1}, {"name": "海边,夜游", "weight": 4},
        ])

    async def test_activity_density_is_safely_normalized(self):
        self.service.cfg.update(activity_density="unknown")
        self.assertEqual(self.service._activity_density(), "balanced")

    async def test_planner_input_includes_activity_pacing_sleep_and_continuity(self):
        self.service.cfg.update({"activity_pool": ["密室逃脱,7", "KTV,6"],
                                 "activity_pool_allow_custom": False,
                                 "activity_density": "balanced"})
        data = await self._input()
        self.assertEqual(data["activity_pool"], [{"name": "密室逃脱", "weight": 7},
                                                 {"name": "KTV", "weight": 6}])
        self.assertEqual(data["activity_density"], "balanced")
        self.assertFalse(data["activity_pool_allow_custom"])
        self.assertNotIn("sleep_policy", data)
        self.assertNotIn("previous_sleep_minutes", data["continuity"])
        self.assertNotIn("suggested_wake_not_before", data["continuity"])
        self.assertNotIn("accumulated_sleep_debt_minutes", data["continuity"])

    async def test_previous_sleep_remains_descriptive_without_wake_or_debt_derivation(self):
        previous_start = self.start - timedelta(days=1)
        self.service._plans()[previous_start.isoformat()] = {
            "status": "complete", "planner_version": "0.8.2",
            "life_day_start": previous_start.isoformat(), "life_day_end": self.start.isoformat(),
            "daily_theme": "夜生活", "daily_style": "随性",
            "timeline": [{"id": "sleep", "kind": "NORMAL", "category": "sleep",
                          "name": "睡觉", "state": "还在睡", "start_at": "2026-10-09T02:00:00+08:00",
                          "end_at": "2026-10-09T04:00:00+08:00"}],
        }
        data = await self._input()
        self.assertTrue(data["continuity"]["has_previous_life_day"])
        self.assertEqual(data["continuity"]["previous_tail"][-1]["category"], "sleep")
        self.assertFalse(data["continuity"]["previous_life_day_ended_awake"])
        forbidden = {"sleep_policy", "sleep_continuity", "previous_sleep_minutes",
                     "sleep_debt_minutes", "accumulated_sleep_debt_minutes",
                     "suggested_wake_not_before", "recovery_sleep_needed",
                     "sleep_minutes_before_boundary", "target_sleep_hours"}
        self.assertFalse(forbidden.intersection(data))
        self.assertFalse(forbidden.intersection(data["continuity"]))

    async def test_sleep_history_does_not_change_exact_protected_window(self):
        previous_start = self.start - timedelta(days=1)
        self.service._plans()[previous_start.isoformat()] = {
            "status": "complete", "planner_version": "0.8.2",
            "life_day_start": previous_start.isoformat(), "life_day_end": self.start.isoformat(),
            "timeline": [{"id": "sleep", "kind": "NORMAL", "category": "sleep",
                          "name": "睡觉", "state": "睡觉", "start_at": "2026-10-09T02:00:00+08:00",
                          "end_at": "2026-10-09T04:00:00+08:00"}],
        }
        data = await self._input([window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")])
        self.assertEqual([(row["start_at"], row["end_at"]) for row in data["protected_windows"]],
                         [("2026-10-09T08:55:00+08:00", "2026-10-09T12:05:00+08:00")])
        self.assertEqual(data["continuity"]["previous_tail"][-1]["category"], "sleep")

    async def test_no_history_explicitly_forbids_invented_previous_night(self):
        data = await self._input()
        prompt = self.service._planner_prompt(data)
        self.assertFalse(data["continuity"]["has_previous_life_day"])
        self.assertIn("没有可用的既往小满生命日历史", data["continuity"]["history_note"])
        self.assertIn("不得编造昨晚/前几天发生过的事实", prompt)

    async def test_pacing_prompt_encourages_sleep_and_rest_without_sleep_debt(self):
        data = await self._input()
        data["activity_density"] = "balanced"
        prompt = self.service._planner_prompt(data)
        self.assertIn("完整覆盖24小时不代表必须保持忙碌", prompt)
        self.assertIn("都可以是长 NORMAL 区块", prompt)
        self.assertIn("正常生命日通常应包含一些睡眠或休息", prompt)
        self.assertIn("睡眠只是普通生活事件", prompt)
        self.assertIn("不要计算或补偿前一日睡眠时长", prompt)
        self.assertIn("不要因为晚睡就推断必须晚起", prompt)
        self.assertIn("不要因为上一生命日曾在睡觉就强制新生命日继续睡", prompt)

    async def test_prompt_gives_protected_windows_structural_priority_over_sleep_history(self):
        prompt = self.service._planner_prompt(await self._input())
        self.assertIn("protected_windows 是结构性的规划锚点，优先级高于睡眠历史", prompt)
        self.assertIn("睡眠历史绝不能占用、改变或使保护窗失效", prompt)
        self.assertIn("不存在睡眠推导出的起床时间", prompt)

    async def test_prompt_distinguishes_free_sleep_from_mixed_protected_bridge(self):
        prompt = self.service._planner_prompt(await self._input())
        self.assertIn("free_window 内，较长睡眠通常应作为独立 category=sleep 的 NORMAL 事件", prompt)
        self.assertIn("protected_window 的 BRIDGE 必须保持一个不可拆分的顶层事件", prompt)
        self.assertIn("睡眠→醒来→慢启动", prompt)
        self.assertIn("通常使用 category=mixed", prompt)
        self.assertIn("不得为了避免 mixed 而提前叫醒小满或把 BRIDGE 拆开", prompt)

    async def test_sleep_normal_and_mixed_bridge_categories_are_accepted(self):
        self.fish.windows = [window("2026-10-09T09:00:00+08:00", "2026-10-09T12:00:00+08:00")]
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        normal = next(row for row in timeline if row["kind"] == "NORMAL")
        bridge = next(row for row in timeline if row["kind"] == "BRIDGE")
        normal["category"] = "sleep"
        bridge["category"] = "mixed"
        self.assertTrue(self.service.validate_timeline(data, timeline)[0])

    async def test_new_plans_require_valid_category(self):
        data = await self._input()
        timeline = valid_response_for(data)["timeline"]
        missing = [dict(row) for row in timeline]
        missing[0].pop("category")
        self.assertFalse(self.service.validate_timeline(data, missing)[0])
        invalid = [dict(row) for row in timeline]
        invalid[0]["category"] = "nap"
        self.assertFalse(self.service.validate_timeline(data, invalid)[0])
        self.assertTrue(self.service.validate_timeline(data, timeline)[0])
        timeline[0]["category"] = "sleep"
        self.assertTrue(self.service.validate_timeline(data, timeline)[0])

    async def test_legacy_categoryless_plan_still_loads_refreshes_and_sends(self):
        self.context.output_factory = None
        plan = {"schema_version": 2, "planner_version": "0.8.1",
                "life_day_start": self.start.isoformat(),
                "life_day_end": (self.start + timedelta(days=1)).isoformat(),
                "timezone": "Asia/Shanghai", "daily_theme": "旧计划", "daily_style": "随性",
                "status": "complete", "timeline": [{
                    "id": "old", "kind": "NORMAL", "start_at": self.start.isoformat(),
                    "end_at": (self.start + timedelta(hours=1)).isoformat(),
                    "name": "睡觉", "state": "睡觉", "broadcast_message": "旧消息",
                }], "deliveries": [{"id": "old", "kind": "NORMAL", "timeline_id": "old",
                                    "trigger_at": self.start.isoformat(), "message": "旧消息"}],
                }
        self.service._plans()[self.start.isoformat()] = plan
        self.assertIs(self.service.current_plan(self.start), plan)
        self.assertTrue(await self.service.refresh())
        history = self.service._history(self.start + timedelta(days=1), 5, True)
        self.assertEqual(history[0]["previous_tail"][-1]["category"], "sleep")
        result = await self.service.send_due(self.start)
        self.assertEqual(result["success_count"], 1)

    async def test_recent_xiaoman_plans_are_limited_by_recent_days(self):
        for index in range(7):
            start = self.start - timedelta(days=index + 1)
            self.service._plans()[start.isoformat()] = {
                "status": "complete", "life_day_start": start.isoformat(),
                "life_day_end": (start + timedelta(days=1)).isoformat(),
                "daily_theme": f"theme-{index}", "daily_style": "style",
                "timeline": [{"name": "sleep"}, {"name": f"final-{index}"}],
            }
        data = await self._input()
        self.assertEqual(len(data["recent_life_days"]), 5)
        self.assertEqual(data["recent_life_days"][0]["daily_theme"], "theme-0")
        self.assertEqual(data["recent_life_days"][0]["previous_final_events"][-1], "final-0")

    async def test_one_generation_persists_complete_timeline_and_messages(self):
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)
        plan = self.service.current_plan(self.start)
        self.assertEqual(plan["schema_version"], 2)
        self.assertTrue(plan["daily_theme"] and plan["daily_style"])
        self.assertTrue(all(event["message"] for event in plan["deliveries"]))

    async def test_planner_does_not_rely_on_ineffective_provider_kwargs(self):
        self.context.provider_type = "openai_chat_completion"
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)
        self.assertNotIn("response_format", self.context.llm_calls[0])
        self.assertNotIn("max_tokens", self.context.llm_calls[0])

    async def test_complete_markdown_json_fence_is_unwrapped(self):
        planner_input = await self._input()
        raw = json.dumps(valid_response_for(planner_input), ensure_ascii=False)
        self.context.raw_output = f"```json\n{raw}\n```"
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)

    async def test_malformed_json_fails_once_without_persisting_partial_plan(self):
        self.context.raw_output = '{"daily_theme":"x" "daily_style":"y","timeline":[]}'
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.context.llm_calls), 1)
        self.assertNotEqual(self.service._plans().get(self.start.isoformat(), {}).get("status"), "complete")

    async def test_unterminated_json_string_logs_only_bounded_diagnostics(self):
        secret = "PRIVATE-RAW-TAIL-" + ("x" * 500)
        self.context.raw_output = '{"daily_theme":"x","daily_style":"y","timeline":[{"state":"' + secret
        with patch.object(sys.modules[ScheduleBroadcastService.__module__].logger, "warning") as warning:
            result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.context.llm_calls), 1)
        warning.assert_called_once()
        diagnostic = str(warning.call_args)
        self.assertIn("raw_length", diagnostic)
        self.assertIn("error_line", diagnostic)
        self.assertIn("error_column", diagnostic)
        self.assertIn("error_pos", diagnostic)
        self.assertIn("raw_tail", diagnostic)
        self.assertNotIn(secret, diagnostic)
        self.assertLessEqual(len(warning.call_args.args[-1]), 240)

    async def test_planner_prompt_requires_compact_json_and_hard_field_caps(self):
        prompt = self.service._planner_prompt(await self._input())
        self.assertIn("紧凑 JSON", prompt)
        self.assertIn("不要 Markdown、代码围栏、解释", prompt)
        self.assertIn("daily_theme 不超过60字符", prompt)
        self.assertIn("daily_style 不超过40字符", prompt)
        self.assertIn("name 不超过48字符", prompt)
        self.assertIn("NORMAL state 不超过120字符", prompt)
        self.assertIn("BRIDGE state 不超过240字符", prompt)
        self.assertIn("不写小说式叙述", prompt)

    async def test_daily_theme_and_style_character_caps(self):
        self.assertTrue(self.service.validate_daily_metadata({"daily_theme": "t" * 60,
                                                              "daily_style": "s" * 40})[0])
        self.assertFalse(self.service.validate_daily_metadata({"daily_theme": "t" * 61,
                                                               "daily_style": "s" * 40})[0])
        self.assertFalse(self.service.validate_daily_metadata({"daily_theme": "t" * 60,
                                                               "daily_style": "s" * 41})[0])

    async def test_entry_name_and_normal_state_character_caps(self):
        planner_input = await self._input()
        timeline = valid_response_for(planner_input)["timeline"]
        normal = next(row for row in timeline if row["kind"] == "NORMAL")
        normal["name"] = "n" * 48
        normal["state"] = "s" * 120
        self.assertTrue(self.service.validate_timeline(planner_input, timeline)[0])
        normal["name"] += "x"
        self.assertFalse(self.service.validate_timeline(planner_input, timeline)[0])
        normal["name"] = "n" * 48
        normal["state"] += "x"
        self.assertFalse(self.service.validate_timeline(planner_input, timeline)[0])

    async def test_bridge_state_character_cap(self):
        planner_input = await self._input([window("2026-10-09T09:00:00+08:00",
                                                   "2026-10-09T12:00:00+08:00")])
        timeline = valid_response_for(planner_input)["timeline"]
        bridge = next(row for row in timeline if row["kind"] == "BRIDGE")
        bridge["state"] = "s" * 240
        self.assertTrue(self.service.validate_timeline(planner_input, timeline)[0])
        bridge["state"] += "x"
        self.assertFalse(self.service.validate_timeline(planner_input, timeline)[0])

    async def test_oversized_valid_json_fails_once_without_complete_plan(self):
        self.context.output_factory = lambda prompt: {
            **valid_response_for(json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1])),
            "daily_theme": "t" * 61,
        }
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.context.llm_calls), 1)
        self.assertNotEqual(self.service._plans().get(self.start.isoformat(), {}).get("status"), "complete")

    async def test_fatfish_block_still_makes_zero_llm_calls(self):
        self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "blocked_by_fat_fish")
        self.assertEqual(len(self.context.llm_calls), 0)

    async def test_planner_call_is_deferred_during_fatfish_peak_then_runs_offpeak(self):
        self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        blocked = await self.service.generate_life_day(self.start)
        self.assertEqual(blocked["status"], "deferred")
        self.assertEqual(blocked["reason"], "blocked_by_fat_fish")
        self.assertEqual(len(self.context.llm_calls), 0)
        self.assertNotIn(self.start.isoformat(), self.service.state["generation_attempts"])

        self.day.now = datetime(2026, 10, 9, 12, 6, tzinfo=TZ)
        generated = await self.service.generate_life_day(self.start)
        self.assertEqual(generated["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)

    async def test_manual_admin_regenerate_bypasses_peak_only_with_both_settings(self):
        fish = self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        results = await self.service.regenerate(
            "next", now=self.day.now, allow_wallet_bypass=True)
        self.assertEqual(results[0]["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)
        self.assertTrue(results[0]["wallet_bypass_used"])

        self.service.reset("next", now=self.day.now)
        self.context.llm_calls.clear()
        self.service.cfg["admin_regenerate_bypass_fat_fish"] = False
        blocked = await self.service.regenerate(
            "next", now=self.day.now, allow_wallet_bypass=True)
        self.assertEqual(blocked[0]["reason"], "blocked_by_fat_fish")
        self.assertEqual(len(self.context.llm_calls), 0)

        fish.config["admins_bypass"] = True
        self.context.get_provider_by_id = lambda _provider_id: None
        blocked = await self.service.regenerate(
            "next", now=self.day.now, allow_wallet_bypass=True)
        self.assertEqual(blocked[0]["reason"], "blocked_by_fat_fish")
        self.assertEqual(len(self.context.llm_calls), 0)

        self.service.cfg["admin_regenerate_bypass_fat_fish"] = True
        fish = next(row.star_cls for row in self.context.stars
                    if row.name == "astrbot_plugin_fat_fish_wallet")
        fish.config["admins_bypass"] = False
        blocked = await self.service.regenerate(
            "next", now=self.day.now, allow_wallet_bypass=True)
        self.assertEqual(blocked[0]["reason"], "blocked_by_fat_fish")
        self.assertEqual(len(self.context.llm_calls), 0)

    async def test_startup_tick_makes_no_planner_call_during_peak(self):
        self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        await self.service.tick()
        self.assertEqual(len(self.context.llm_calls), 0)
        self.assertEqual(self.service.state["generation_attempts"], {})

    async def test_manual_regenerate_does_not_bypass_fatfish_peak_guard(self):
        self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        results = await self.service.regenerate("current", now=self.day.now)
        self.assertEqual(results[0]["status"], "deferred")
        self.assertEqual(len(self.context.llm_calls), 0)

    async def test_always_allow_and_always_block_override_planner_cost_gate(self):
        fish = self._use_config_only_fatfish(manual_override="always_allow")
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        allowed = await self.service.generate_life_day(self.start)
        self.assertEqual(allowed["status"], "generated")
        self.assertEqual(len(self.context.llm_calls), 1)

        self.service.reset("current", now=self.day.now)
        self.context.llm_calls.clear()
        fish.config["manual_override"] = "always_block"
        self.day.now = datetime(2026, 10, 9, 12, 30, tzinfo=TZ)
        manual = await self.service.regenerate(
            "current", now=self.day.now, allow_wallet_bypass=True)
        self.assertEqual(manual[0]["status"], "generated")
        self.assertTrue(manual[0]["wallet_bypass_used"])
        self.assertEqual(len(self.context.llm_calls), 1)

        self.service.reset("current", now=self.day.now)
        self.context.llm_calls.clear()
        await self.service.tick()
        self.assertEqual(len(self.context.llm_calls), 0)

    async def test_direct_service_generation_never_inherits_admin_bypass(self):
        self._use_config_only_fatfish()
        self.day.now = datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
        result = await self.service.generate_life_day(self.start)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(len(self.context.llm_calls), 0)

    async def test_derive_refresh_and_send_due_do_not_call_llm(self):
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        await self.service.generate_life_day(self.start)
        calls = len(self.context.llm_calls)
        await self.service.refresh()
        await self.service.send_due(self.start)
        self.assertEqual(len(self.context.llm_calls), calls)

    async def test_bridge_exit_and_normal_same_timestamp_only_one_broadcast(self):
        timeline = [{"id": "b", "kind": "BRIDGE", "start_at": "s", "end_at": "x",
                     "enter_message": "in", "exit_message": "out"},
                    {"id": "n", "kind": "NORMAL", "start_at": "x", "end_at": "y",
                     "broadcast_message": "duplicate"}]
        events = self.service._derive_deliveries(timeline)
        self.assertEqual([row["id"] for row in events], ["b-ENTER", "b-EXIT"])

    async def test_refresh_preserves_successful_delivery_when_event_time_changes(self):
        timeline = [{"id": "event", "kind": "NORMAL", "start_at": "new", "end_at": "later",
                     "broadcast_message": "updated"}]
        old = [{"id": "event", "trigger_at": "old", "sent": True,
                "delivered_umos": ["physical-qq-group:bot:115"]}]
        event = self.service._derive_deliveries(timeline, old)[0]
        self.assertTrue(event["sent"])
        self.assertEqual(event["delivered_umos"], ["physical-qq-group:bot:115"])

    def test_old_v07_store_is_preserved_and_not_migrated(self):
        old = Path(self.tmp.name) / "schedule_broadcast_state.json"
        old.write_text('{"entries":[{"sent":true}]}', encoding="utf-8")
        service = ScheduleBroadcastService(self.context, {}, self.tmp.name, time_awareness=self.day, fat_fish=self.fish)
        self.assertEqual(service.state["plans"], {})
        self.assertTrue(old.exists())
        service._save()
        self.assertEqual(json.loads(old.read_text(encoding="utf-8")), {"entries": [{"sent": True}]})

    async def test_generation_failure_is_throttled_not_retried_each_poll(self):
        self.context.output_factory = lambda _prompt: {"timeline": []}
        first = await self.service.generate_life_day(self.start)
        second = await self.service.generate_life_day(self.start)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "throttled")
        self.assertEqual(len(self.context.llm_calls), 1)

    async def test_planner_failure_retry_seconds_setting_is_respected(self):
        self.service.cfg["planner_failure_retry_seconds"] = 60
        self.context.output_factory = lambda _prompt: {"timeline": []}
        failed = await self.service.generate_life_day(self.start)
        self.assertEqual(failed["status"], "failed")
        self.day.now = self.start + timedelta(seconds=59)
        throttled = await self.service.generate_life_day(self.start)
        self.assertEqual(throttled["status"], "throttled")
        self.assertEqual(len(self.context.llm_calls), 1)
        self.day.now = self.start + timedelta(seconds=60)
        retried = await self.service.generate_life_day(self.start)
        self.assertEqual(retried["status"], "failed")
        self.assertEqual(len(self.context.llm_calls), 2)

    async def test_invalid_planner_retry_setting_falls_back_and_negative_clamps_to_zero(self):
        self.service.cfg["planner_failure_retry_seconds"] = "not-a-number"
        self.assertEqual(self.service._int("planner_failure_retry_seconds", 1800), 1800)
        self.service.cfg["planner_failure_retry_seconds"] = -20
        self.assertEqual(self.service._int("planner_failure_retry_seconds", 1800), 0)

    async def test_status_reports_bypass_and_broadcast_settings_without_config_dump(self):
        self.service.cfg.update(peak_guard_before_minutes=7, peak_guard_after_minutes=3,
                                admin_regenerate_bypass_fat_fish=True, dry_run=True)
        state = await self.service.status(self.start)
        self.assertEqual((state["peak_guard_before_minutes"], state["peak_guard_after_minutes"]), (7, 3))
        self.assertTrue(state["admin_regenerate_bypass_fat_fish"])
        self.assertTrue(state["fat_fish_admins_bypass"])
        self.assertTrue(state["dry_run"])
        self.assertNotIn("config", state)

    async def test_automatic_bootstrap_and_pregeneration_do_not_repeat_each_tick(self):
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        await self.service.tick()
        self.assertEqual(len(self.context.llm_calls), 2)  # current bootstrap + next life day
        await self.service.tick()
        self.assertEqual(len(self.context.llm_calls), 2)

    async def test_generation_lock_prevents_duplicate_concurrent_plans(self):
        self.context.output_factory = lambda prompt: valid_response_for(
            json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1]))
        results = await asyncio.gather(self.service.generate_life_day(self.start),
                                       self.service.generate_life_day(self.start))
        self.assertEqual(len(self.context.llm_calls), 1)
        self.assertEqual({item["status"] for item in results}, {"generated", "exists"})

    async def test_dry_run_and_explicit_simulation(self):
        self.context.rows = [types.SimpleNamespace(user_id="qq:FriendMessage:one", platform_id="qq")]
        plan = {"timezone": "Asia/Shanghai", "deliveries": [{"id": "e", "trigger_at": self.start.isoformat(),
                 "message": "hello", "sent": False, "expired": False, "delivered_umos": []}]}
        self.service._plans()[self.start.isoformat()] = plan
        self.service.cfg["dry_run"] = True
        result = await self.service.send_due(self.start)
        self.assertEqual(result["success_count"], 0)
        self.assertFalse(self.context.sent)
        simulated, error = await self.service.simulate_time("04:00", self.start)
        self.assertFalse(error)
        self.assertEqual(simulated["success_count"], 1)
        self.assertEqual(len(self.context.sent), 1)
        self.assertFalse(plan["deliveries"][0]["sent"])

    async def test_partial_failure_retries_and_false_is_failure(self):
        self.context.rows = [types.SimpleNamespace(user_id="x:FriendMessage:a", platform_id="x"),
                             types.SimpleNamespace(user_id="x:FriendMessage:b", platform_id="x")]
        plan = {"timezone": "Asia/Shanghai", "deliveries": [{"id": "e", "trigger_at": self.start.isoformat(),
                 "message": "hello", "sent": False, "expired": False, "delivered_umos": []}]}
        self.service._plans()[self.start.isoformat()] = plan
        self.context.false.add("x:FriendMessage:a")
        self.context.fail.add("x:FriendMessage:b")
        result = await self.service.send_due(self.start)
        self.assertEqual(result["failure_count"], 2)
        self.assertFalse(plan["deliveries"][0]["sent"])
        self.context.false.clear()
        self.context.fail.clear()
        result = await self.service.send_due(self.start)
        self.assertEqual(result["success_count"], 2)
        self.assertTrue(plan["deliveries"][0]["sent"])

    async def test_successful_group_is_not_resent_when_another_group_fails(self):
        self.context.rows = [types.SimpleNamespace(user_id="qq:FriendMessage:a", platform_id="qq"),
                             types.SimpleNamespace(user_id="qq:FriendMessage:b", platform_id="qq")]
        self.context.fail.add("qq:FriendMessage:b")
        event = {"id": "partial", "trigger_at": self.start.isoformat(), "message": "hi",
                 "sent": False, "expired": False, "delivered_umos": []}
        self.service._plans()[self.start.isoformat()] = {"timezone": "Asia/Shanghai", "deliveries": [event]}
        first = await self.service.send_due(self.start)
        self.assertEqual(first["success_count"], 1)
        self.assertFalse(event["sent"])
        self.context.fail.clear()
        second = await self.service.send_due(self.start)
        self.assertEqual(second["success_count"], 1)
        self.assertEqual([umo for umo, _ in self.context.sent], ["qq:FriendMessage:a", "qq:FriendMessage:b"])
        self.assertTrue(event["sent"])

    async def test_legacy_umo_delivery_record_suppresses_group_alias_resend(self):
        self.context.rows = [types.SimpleNamespace(user_id="bot:GroupMessage:1153387215", platform_id="bot")]
        event = {"id": "already-delivered", "trigger_at": self.start.isoformat(), "message": "hi",
                 "sent": False, "expired": False,
                 "delivered_umos": ["bot:GroupMessage:979675497_1153387215"]}
        self.service._plans()[self.start.isoformat()] = {"timezone": "Asia/Shanghai", "deliveries": [event]}
        result = await self.service.send_due(self.start)
        self.assertEqual(result["success_count"], 0)
        self.assertFalse(self.context.sent)
        self.assertTrue(event["sent"])

    async def test_no_targets_leaves_event_pending(self):
        self.context.rows = []
        event = {"id": "no-target", "trigger_at": self.start.isoformat(), "message": "hi",
                 "sent": False, "expired": False, "delivered_umos": []}
        self.service._plans()[self.start.isoformat()] = {"timezone": "Asia/Shanghai", "deliveries": [event]}
        result = await self.service.send_due(self.start)
        self.assertEqual(result["target_count"], 0)
        self.assertFalse(event["sent"])

    async def test_qq_physical_group_dedup_and_platform_instances(self):
        self.context.rows = [
            types.SimpleNamespace(user_id="bot-a:GroupMessage:111_1153387215", platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215", platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-b:GroupMessage:222_1153387215", platform_id="bot-b"),
            types.SimpleNamespace(user_id="bot-b:GroupMessage:1153387215", platform_id="bot-b"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:invalid_x", platform_id="bot-a"),
        ]
        targets = await self.service.targets()
        self.assertEqual(len(targets), 3)
        self.assertEqual(len({state["key"] for states in self.service._target_delivery.values() for state in states}), 3)

    async def test_alias_denylist_has_priority(self):
        self.context.rows = [types.SimpleNamespace(user_id="bot-a:GroupMessage:111_1153387215", platform_id="bot-a"),
                             types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215", platform_id="bot-a")]
        self.service.cfg["allowlist_umos"] = ["bot-a:GroupMessage:1153387215"]
        self.service.cfg["denylist_umos"] = ["bot-a:GroupMessage:111_1153387215"]
        self.assertEqual(await self.service.targets(), [])

    async def test_group_private_and_allowlist_controls_remain_active(self):
        self.context.rows = [types.SimpleNamespace(user_id="qq:GroupMessage:100", platform_id="qq"),
                             types.SimpleNamespace(user_id="qq:FriendMessage:200", platform_id="qq")]
        self.service.cfg["send_groups"] = False
        self.assertEqual(await self.service.targets(), ["qq:FriendMessage:200"])
        self.service.cfg["send_groups"] = True
        self.service.cfg["send_private"] = False
        self.assertEqual(await self.service.targets(), ["qq:GroupMessage:100"])
        self.service.cfg.update(send_private=True, allowlist_umos=["qq:FriendMessage:200"])
        self.assertEqual(await self.service.targets(), ["qq:FriendMessage:200"])

    async def test_persistence_retry_and_expiration(self):
        self.context.rows = [types.SimpleNamespace(user_id="qq:FriendMessage:one", platform_id="qq")]
        plan = {"timezone": "Asia/Shanghai", "deliveries": [{"id": "e", "trigger_at": self.start.isoformat(),
                 "message": "hello", "sent": False, "expired": False, "delivered_umos": []}]}
        self.service._plans()[self.start.isoformat()] = plan
        self.context.fail.add("qq:FriendMessage:one")
        await self.service.send_due(self.start)
        self.assertFalse(plan["deliveries"][0]["sent"])
        self.context.fail.clear()
        await self.service.send_due(self.start)
        self.assertTrue(plan["deliveries"][0]["sent"])
        late = self.start + timedelta(minutes=2)
        plan["deliveries"].append({"id": "late", "trigger_at": self.start.isoformat(), "message": "late",
                                   "sent": False, "expired": False, "delivered_umos": []})
        await self.service.send_due(late)
        self.assertTrue(plan["deliveries"][1]["expired"])


class AdapterAndBridgeTests(unittest.TestCase):
    def test_xiaoman_gear_schema_exposes_owned_settings_and_defaults(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["schedule_broadcast"]["items"]
        expected = {
            "enable": False, "send_groups": True, "send_private": True,
            "allowlist_umos": [], "denylist_umos": [], "provider_id": "",
            "peak_guard_before_minutes": 5, "peak_guard_after_minutes": 5,
            "activity_pool_allow_custom": True, "activity_density": "balanced",
            "admin_regenerate_bypass_fat_fish": True, "poll_seconds": 15,
            "grace_seconds": 60, "planner_failure_retry_seconds": 1800,
            "max_message_chars": 80, "dry_run": False,
            "planner_prompt": "尊重给定世界观、主题池和近期生活日历史；一天要有真实变化，也允许休息和宅家。",
        }
        for key, default in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, items)
                self.assertEqual(items[key]["default"], default)
                self.assertTrue(items[key].get("description"))
        self.assertNotIn("schema_version", items)
        self.assertNotIn("planner_version", items)
        self.assertNotIn("sleep_target_hours", items)
        self.assertEqual(items["activity_pool"]["type"], "list")
        self.assertIn("密室逃脱,7", items["activity_pool"]["default"])
        self.assertEqual(items["activity_density"]["options"], ["relaxed", "balanced", "busy"])

    def test_timeawareness_planner_context_reads_worldview_pools_and_random_weather(self):
        config = {"daily_schedule": {"ai_daily": {
            "generation_time": "-04:00", "worldview": "wide worldview", "use_persona": False,
            "random_weather_enabled": True,
            "adaptive": {"theme_pool": ["theme"], "style_pool": ["style"],
                          "allow_custom_theme": False, "recent_days": 4,
                          "state_continuity_enabled": True}}}}
        class Plugin:
            def __init__(self):
                self.config = config
                self.time_context = types.SimpleNamespace(
                    now=lambda: datetime(2026, 10, 9, 10, tzinfo=TZ),
                    facts=types.SimpleNamespace(collect=lambda **kwargs: types.SimpleNamespace(
                        workday=types.SimpleNamespace(kind="workday", available=True, value="办公日"),
                        now=kwargs["now"])))
                self.weather_sensor = types.SimpleNamespace(
                    daily_forecast=lambda day: {"date": day.isoformat(), "summary": "晴"})
        plugin = Plugin()
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(
            name="time_awareness", activated=True, star_cls=plugin)])
        adapter = TimeAwarenessAdapter(context)
        start, end = datetime(2026, 10, 9, 4, tzinfo=TZ), datetime(2026, 10, 10, 4, tzinfo=TZ)
        result = asyncio.run(adapter.planner_context(start, end))
        self.assertEqual(result["worldview"], "wide worldview")
        self.assertFalse(result["use_persona"])
        self.assertEqual(result["theme_pool"], ["theme"])
        self.assertEqual(result["style_pool"], ["style"])
        self.assertFalse(result["allow_custom_theme"])
        self.assertEqual(result["adaptive"]["recent_days"], 4)
        self.assertEqual([row["date"] for row in result["weather"]], ["2026-10-09", "2026-10-10"])

    def test_generation_time_0330_changes_dynamic_life_day(self):
        plugin = types.SimpleNamespace(
            config={"daily_schedule": {"ai_daily": {"generation_time": "-03:30"}}},
            time_context=types.SimpleNamespace(now=lambda: datetime(2026, 10, 9, 3, 45, tzinfo=TZ)))
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(
            name="time_awareness", activated=True, star_cls=plugin)])
        adapter = TimeAwarenessAdapter(context)
        window = adapter.life_day_window()
        self.assertEqual(adapter.get_generation_boundary()["clock"], "03:30")
        self.assertEqual(window["start"], datetime(2026, 10, 9, 3, 30, tzinfo=TZ))
        self.assertEqual(window["end"], datetime(2026, 10, 10, 3, 30, tzinfo=TZ))

    def test_fatfish_bridge_is_read_only_and_uses_effective_period_policy(self):
        class Fish:
            # Public shape from official Fat Fish Wallet v1.1.1: config only.
            config = {"enabled": True, "timezone": "Asia/Shanghai",
                      "peak_periods": "09:00-12:00,14:00-18:00",
                      "peak_weekdays": "0,1,2,3,4,5,6",
                      "affected_providers": "deepseek", "gate_when_provider_unknown": True,
                      "manual_override": "auto", "admins_bypass": True}

            def _cfg(self, *_args, **_kwargs):
                raise AssertionError("private _cfg must not be called")

            def _periods(self, *_args, **_kwargs):
                raise AssertionError("private _periods must not be called")

            def _weekdays(self, *_args, **_kwargs):
                raise AssertionError("private _weekdays must not be called")

            def _provider_affected(self, *_args, **_kwargs):
                raise AssertionError("private _provider_affected must not be called")

        fish = Fish()
        original_config = json.loads(json.dumps(fish.config))
        provider = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(
            id="planner-provider", model="deepseek-chat", type="llm"))
        context = types.SimpleNamespace(
            get_all_stars=lambda: [types.SimpleNamespace(
                name="astrbot_plugin_fat_fish_wallet", activated=True, star_cls=fish)],
            get_provider_by_id=lambda provider_id: provider if provider_id == "planner-provider" else None)
        bridge = FatFishBridge(context, FakeDay())
        start, end = datetime(2026, 10, 9, 4, tzinfo=TZ), datetime(2026, 10, 10, 4, tzinfo=TZ)
        calendar = [{"date": "2026-10-09", "kind": "adjusted"}]
        windows = bridge.effective_peak_windows(start, end, "planner-provider", calendar)
        self.assertEqual([(item["start_at"].strftime("%H:%M"), item["end_at"].strftime("%H:%M"))
                          for item in windows], [("09:00", "12:00"), ("14:00", "18:00")])
        peak_policy = bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="planner-provider")
        self.assertTrue(peak_policy["provider_affected"])
        self.assertEqual(peak_policy["state"], "peak")
        self.assertFalse(peak_policy["allowed"])
        self.assertTrue(peak_policy["admins_bypass"])
        offpeak_policy = bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 12, 6, tzinfo=TZ), provider_id="planner-provider")
        self.assertEqual(offpeak_policy["state"], "offpeak")
        self.assertTrue(offpeak_policy["allowed"])
        self.assertTrue(bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="missing")["provider_affected"])
        for day_kind in ("weekend", "holiday"):
            self.assertEqual(bridge.effective_peak_windows(
                start, end, "planner-provider", [{"date": "2026-10-09", "kind": day_kind}]), [])
        self.assertEqual(fish.config, original_config)
        self.assertFalse(hasattr(bridge, "install"))
        for key, value in (("enabled", False), ("manual_override", "always_allow"),
                           ("manual_override", "always_block"), ("affected_providers", "openai")):
            old_value = fish.config.get(key)
            fish.config[key] = value
            self.assertEqual(bridge.effective_peak_windows(start, end, "planner-provider", calendar), [])
            if old_value is None:
                fish.config.pop(key, None)
            else:
                fish.config[key] = old_value

        fish.config["affected_providers"] = ""
        self.assertFalse(bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="planner-provider")["provider_affected"])
        fish.config["affected_providers"] = "*"
        self.assertTrue(bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="missing")["provider_affected"])
        fish.config["affected_providers"] = "openai"
        not_affected = bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="planner-provider")
        self.assertFalse(not_affected["provider_affected"])
        self.assertTrue(not_affected["allowed"])
        fish.config["affected_providers"] = "openai, DEEPSEEK"
        self.assertTrue(bridge.get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="planner-provider")["provider_affected"])

    def test_fatfish_unknown_provider_uses_gate_when_unknown_setting(self):
        class Fish:
            config = {"enabled": True, "timezone": "Asia/Shanghai", "manual_override": "auto",
                      "peak_periods": "09:00-12:00", "peak_weekdays": "0,1,2,3,4,5,6",
                      "affected_providers": "deepseek", "gate_when_provider_unknown": False}
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(
            name="astrbot_plugin_fat_fish_wallet", activated=True, star_cls=Fish())],
            get_provider_by_id=lambda _provider_id: (_ for _ in ()).throw(RuntimeError("unknown")))
        result = FatFishBridge(context, FakeDay()).get_wallet_policy(
            at=datetime(2026, 10, 9, 10, tzinfo=TZ), provider_id="missing")
        self.assertFalse(result["provider_affected"])
        self.assertTrue(result["allowed"])

    def test_rolling_day_prompt_bridge_is_not_installed_or_imported_by_main(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("RollingDayBridge", source)
        self.assertFalse((ROOT / "services" / "rolling_day_bridge.py").exists())


if __name__ == "__main__":
    unittest.main()
