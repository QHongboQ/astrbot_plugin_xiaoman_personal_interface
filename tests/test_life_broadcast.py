"""Focused contracts for the optional Life Scheduler adapter."""
from __future__ import annotations

import asyncio
import json
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

from test_photo_tool import MAIN_MODULE  # installs the lightweight public API stubs/package path
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.life_broadcast import (
    LIFE_SCHEDULER_NAME,
    LifeBroadcastService,
    parse_schedule,
)


ROW = "08:55｜地点：学校｜事项：上午课程｜细节：今天第一节课有点困"
ROW2 = "12:10|地点：家里|事项：吃午饭|细节：想吃咖喱饭"
LEGACY_ROW = "08:55｜学校：上午课程｜准备上课"


class FakeContext:
    def __init__(self, schedule=f"{ROW}\n{ROW2}", targets=None, outputs=None):
        self.schedule = schedule
        self.targets_list = targets if targets is not None else ["qq:GroupMessage:1", "qq:FriendMessage:2"]
        self.stars = [types.SimpleNamespace(name=LIFE_SCHEDULER_NAME, activated=True, star_cls=Life(self))]
        async def get_conversations():
            return [types.SimpleNamespace(user_id=x) for x in self.targets_list]
        self.conversation_manager = types.SimpleNamespace(get_conversations=get_conversations)
        self.persona_manager = types.SimpleNamespace(get_default_persona_v3=self.persona)
        self.outputs = list(outputs or [json.dumps({"E1": "我准备去学校上上午课程", "E2": "我去家里吃午饭啦"}, ensure_ascii=False)])
        self.llm_calls = []
        self.sent = []
        self.provider_calls = []

    async def persona(self):
        return {"name": "小满", "prompt": "persona-dict-original"}

    def get_all_stars(self):
        return self.stars

    async def get_current_chat_provider_id(self, umo):
        self.provider_calls.append(umo)
        return "provider"

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        return types.SimpleNamespace(completion_text=self.outputs.pop(0))

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class Life:
    def __init__(self, ctx): self.ctx = ctx; self.flags = []
    async def get_life_context(self, **kwargs):
        self.flags.append(kwargs)
        return self.ctx.schedule


class LifeBroadcastTests(unittest.IsolatedAsyncioTestCase):
    FIXED_NOW = datetime(2025, 2, 3, 3, 40)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = FakeContext()
        self.svc = LifeBroadcastService(self.ctx, {"life_broadcast": {"enable": True}}, self.tmp.name)

    async def refresh(self, **kwargs):
        kwargs.setdefault("now", self.FIXED_NOW)
        return await self.svc.refresh(**kwargs)

    def test_discovery_requires_exact_name(self):
        self.ctx.stars.insert(0, types.SimpleNamespace(name="other", activated=True, star_cls=object()))
        self.assertIs(self.svc.discover(), self.ctx.stars[1].star_cls)

    def test_discovery_rejects_inactive(self):
        self.ctx.stars = [types.SimpleNamespace(name=LIFE_SCHEDULER_NAME, activated=False, star_cls=object())]
        self.assertIsNone(self.svc.discover())

    def test_discovery_rejects_missing_instance(self):
        self.ctx.stars = [types.SimpleNamespace(name=LIFE_SCHEDULER_NAME, activated=True, star_cls=None)]
        self.assertIsNone(self.svc.discover())

    async def test_read_uses_allow_generate_false(self):
        await self.svc.read_schedule()
        self.assertEqual(self.ctx.stars[0].star_cls.flags, [{"allow_generate": False}])

    def test_fullwidth_parser(self):
        self.assertEqual(parse_schedule(ROW)[0]["activity"], "上午课程")

    def test_ascii_parser(self):
        self.assertEqual(parse_schedule(ROW2)[0]["location"], "家里")

    def test_malformed_row_skips_only_bad_row(self):
        self.assertEqual(len(parse_schedule("bad\n" + ROW)), 1)

    def test_bad_time_skips(self):
        self.assertEqual(parse_schedule("25:99｜地点：X｜事项：Y｜细节：Z"), [])

    def test_labeled_whitespace(self):
        self.assertEqual(parse_schedule(" 08:55 ｜ 地点： 学校 ｜ 事项： 课程 ｜ 细节： 困 ")[0]["location"], "学校")

    def test_legacy_three_field_chinese_colon(self):
        parsed = parse_schedule(LEGACY_ROW)[0]
        self.assertEqual((parsed["time"], parsed["location"], parsed["activity"], parsed["detail"]), ("08:55", "学校", "上午课程", "准备上课"))

    def test_legacy_three_field_ascii_colon(self):
        parsed = parse_schedule("08:55｜学校:上午课程｜准备上课")[0]
        self.assertEqual((parsed["location"], parsed["activity"], parsed["detail"]), ("学校", "上午课程", "准备上课"))

    def test_range_hyphen_uses_start_time(self):
        parsed = parse_schedule("08:55-12:05｜学校：上午课程｜上午都在校")[0]
        self.assertEqual(parsed["time"], "08:55")

    def test_range_en_dash_uses_start_time(self):
        parsed = parse_schedule("08:55–12:05｜学校：上午课程｜上午都在校")[0]
        self.assertEqual(parsed["time"], "08:55")

    def test_offset_zero(self):
        self.assertEqual(self.svc._event_time("08:55", 0), "08:55")

    def test_offset_minus_five(self):
        self.assertEqual(self.svc._event_time("08:55", -5), "08:50")

    def test_offset_wraps_midnight(self):
        self.assertEqual(self.svc._event_time("00:02", -5), "23:57")

    async def test_group_private_target_resolution(self):
        self.assertEqual(len(await self.svc.targets()), 2)

    async def test_group_filter(self):
        self.svc.cfg["send_private"] = False
        self.assertEqual(await self.svc.targets(), ["qq:GroupMessage:1"])

    async def test_private_filter(self):
        self.svc.cfg["send_groups"] = False
        self.assertEqual(await self.svc.targets(), ["qq:FriendMessage:2"])

    async def test_allowlist_filter(self):
        self.svc.cfg.update(target_mode="allowlist", allowlist_umos=["qq:FriendMessage:2"])
        self.assertEqual(await self.svc.targets(), ["qq:FriendMessage:2"])

    async def test_denylist_wins(self):
        self.svc.cfg.update(target_mode="allowlist", allowlist_umos=["qq:FriendMessage:2"], denylist_umos=["qq:FriendMessage:2"])
        self.assertEqual(await self.svc.targets(), [])

    async def test_no_targets(self):
        self.ctx.targets_list.clear()
        self.assertEqual(await self.svc.targets(), [])

    def test_blocked_windows(self):
        from datetime import datetime
        self.assertTrue(self.svc.blocked(datetime(2025, 1, 1, 10, 0)))

    def test_outside_blocked_windows(self):
        from datetime import datetime
        self.assertFalse(self.svc.blocked(datetime(2025, 1, 1, 13, 0)))

    async def test_new_hash_generates_one_batch(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 1)
        self.assertIn("E2", self.ctx.llm_calls[0]["prompt"])

    async def test_past_entries_are_filtered_before_batch_llm(self):
        from datetime import datetime
        self.svc.cfg["blocked_windows"] = []
        self.ctx.schedule = "\n".join(
            [
                "07:30｜地点：家｜事项：早餐｜细节：吃饭",
                "12:10｜地点：家｜事项：午饭｜细节：吃饭",
                "18:20｜地点：街上｜事项：散步｜细节：走走",
                "20:30｜地点：家｜事项：休息｜细节：休息一下",
            ]
        )
        self.ctx.outputs = [json.dumps({"E4": "我准备回家休息"}, ensure_ascii=False)]
        now = datetime(2025, 2, 3, 18, 30)
        await self.svc.refresh(now=now)
        prompt = self.ctx.llm_calls[0]["prompt"]
        self.assertIn("E4 | 20:30", prompt)
        self.assertNotIn("07:30", prompt)
        self.assertNotIn("12:10", prompt)
        self.assertNotIn("18:20", prompt)
        self.assertEqual([entry["time"] for entry in self.svc.state["entries"]], ["20:30"])

    async def test_blocked_node_is_filtered_before_batch_llm(self):
        self.ctx.schedule = "\n".join(
            [
                "08:30｜地点：家｜事项：早餐｜细节：吃饭",
                "10:00｜地点：学校｜事项：上课｜细节：上课",
                "20:30｜地点：家｜事项：休息｜细节：休息",
            ]
        )
        self.ctx.outputs = [json.dumps({"E1": "早餐", "E3": "休息"}, ensure_ascii=False)]
        await self.refresh()
        prompt = self.ctx.llm_calls[0]["prompt"]
        self.assertIn("E1 | 08:30", prompt)
        self.assertNotIn("10:00", prompt)
        self.assertIn("E3 | 20:30", prompt)

    async def test_same_hash_does_not_regenerate(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 1)

    async def test_changed_schedule_hash_regenerates(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.ctx.schedule += "\n18:20｜地点：东门｜事项：散步｜细节：透气"
        self.ctx.outputs.append(json.dumps({"E1": "a", "E2": "b", "E3": "c"}))
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 2)

    async def test_blocked_detection_keeps_pending_then_generates(self):
        state = {"calls": 0}
        def blocking(*_):
            state["calls"] += 1
            return state["calls"] == 1
        self.svc.blocked = blocking
        await self.refresh()
        self.assertEqual(self.ctx.llm_calls, [])
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 1)

    async def test_no_target_no_llm_cost(self):
        self.ctx.targets_list.clear()
        await self.refresh()
        self.assertEqual(self.ctx.llm_calls, [])

    async def test_provider_resolution_uses_first_target(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.ctx.provider_calls, ["qq:FriendMessage:2", "qq:FriendMessage:2"][:1])

    async def test_configured_provider_skips_dynamic_resolution(self):
        self.svc.cfg["provider_id"] = "fixed"
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.ctx.provider_calls, [])
        self.assertEqual(self.ctx.llm_calls[0]["chat_provider_id"], "fixed")

    async def test_persona_prompt_passed_unchanged(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.ctx.llm_calls[0]["system_prompt"], "persona-dict-original")

    async def test_dict_persona_prompt_reaches_llm_system_prompt(self):
        persona = await self.ctx.persona()
        self.assertIsInstance(persona, dict)
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.ctx.llm_calls[0]["system_prompt"], persona["prompt"])

    async def test_no_provider_fails_without_crash(self):
        self.ctx.get_current_chat_provider_id = lambda *_: asyncio.sleep(0, result=None)
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.ctx.llm_calls, [])

    async def test_newline_and_length_sanitization(self):
        self.ctx.outputs = [json.dumps({"E1": "  我准备\n去学校 " + "很" * 100, "E2": "午饭"}, ensure_ascii=False)]
        self.svc.cfg["max_message_chars"] = 12
        self.svc.blocked = lambda *_: False
        await self.refresh()
        message = self.svc.state["entries"][0]["message"]
        self.assertEqual(len(message), 12)
        self.assertNotIn("\n", message)

    async def test_state_persisted_atomically_and_reloaded(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        reloaded = LifeBroadcastService(self.ctx, {"life_broadcast": {}}, self.tmp.name)
        self.assertEqual(reloaded.state["schedule_hash"], self.svc.state["schedule_hash"])

    async def test_reload_same_hash_no_generation(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        again = LifeBroadcastService(self.ctx, {"life_broadcast": {}}, self.tmp.name)
        await again.refresh(now=self.FIXED_NOW)
        self.assertEqual(len(self.ctx.llm_calls), 1)

    async def test_sending_does_not_call_llm(self):
        self.svc.state["entries"] = [{"id": "E1", "time": "08:55", "trigger_time": "08:55", "message": "准备上课", "sent": False}]
        self.svc.blocked = lambda *_: False
        await self.svc.send_due()
        self.assertEqual(self.ctx.llm_calls, [])

    async def test_same_generated_message_is_reused_for_each_target(self):
        from datetime import datetime
        import astrbot.api.event
        class Chain:
            def __init__(self): self.content = ""
            def message(self, text): self.content = text; return self
        astrbot.api.event.MessageChain = Chain
        now = datetime.now().replace(second=0, microsecond=0)
        self.svc.cfg["blocked_windows"] = []
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": now.strftime("%H:%M"), "message": "完全相同", "sent": False}]
        await self.svc.send_due(now)
        self.assertEqual([chain.content for _, chain in self.ctx.sent], ["完全相同", "完全相同"])

    async def test_one_success_one_failure_retries_only_failed_then_completes(self):
        from datetime import datetime
        import astrbot.api.event
        class Chain:
            def __init__(self): self.content = ""
            def message(self, text): self.content = text; return self
        astrbot.api.event.MessageChain = Chain
        fail_group = {"enabled": True}
        attempts = []
        async def send(umo, chain):
            attempts.append(umo)
            self.ctx.sent.append((umo, chain))
            if umo.endswith(":1") and fail_group["enabled"]:
                raise RuntimeError("offline")
        self.ctx.send_message = send
        now = datetime.now().replace(second=0, microsecond=0)
        self.svc.cfg["blocked_windows"] = []
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": now.strftime("%H:%M"), "message": "测试", "sent": False}]
        await self.svc.send_due(now)
        entry = self.svc.state["entries"][0]
        self.assertFalse(entry["sent"])
        self.assertEqual(len(entry["delivered_umos"]), 1)
        delivered_first_attempt = list(entry["delivered_umos"])
        persisted = json.loads(self.svc.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["entries"][0]["delivered_umos"], delivered_first_attempt)
        fail_group["enabled"] = False
        await self.svc.send_due(now)
        self.assertTrue(entry["sent"])
        self.assertEqual(attempts[2:], ["qq:GroupMessage:1"])
        self.assertEqual(entry["delivered_umos"], sorted(["qq:GroupMessage:1", "qq:FriendMessage:2"]))
        self.assertEqual(len(delivered_first_attempt), 1)

    async def test_all_target_sends_fail_sent_stays_false(self):
        from datetime import datetime
        import astrbot.api.event
        class Chain:
            def message(self, text): return self
        astrbot.api.event.MessageChain = Chain
        async def fail(umo, chain):
            raise RuntimeError("offline")
        self.ctx.send_message = fail
        now = datetime.now().replace(second=0, microsecond=0)
        self.svc.cfg["blocked_windows"] = []
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": now.strftime("%H:%M"), "message": "测试", "sent": False}]
        await self.svc.send_due(now)
        entry = self.svc.state["entries"][0]
        self.assertFalse(entry["sent"])
        self.assertEqual(entry["delivered_umos"], [])

    async def test_zero_targets_at_trigger_does_not_mark_sent(self):
        from datetime import datetime
        import astrbot.api.event
        class Chain:
            def message(self, text): return self
        astrbot.api.event.MessageChain = Chain
        now = datetime.now().replace(second=0, microsecond=0)
        self.ctx.targets_list.clear()
        self.svc.cfg["blocked_windows"] = []
        entry = {"id": "E1", "trigger_time": now.strftime("%H:%M"), "message": "测试", "sent": False}
        self.svc.state["entries"] = [entry]
        await self.svc.send_due(now)
        self.assertFalse(entry["sent"])
        self.assertEqual(entry.get("delivered_umos", []), [])
        self.ctx.targets_list.append("qq:GroupMessage:later")
        await self.svc.send_due(now)
        self.assertTrue(entry["sent"])

    async def test_invalid_json_has_at_most_one_repair(self):
        self.ctx.outputs = ["not json", json.dumps({"E1": "a", "E2": "b"})]
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 2)

    async def test_invalid_json_failure_does_not_retry_forever(self):
        self.ctx.outputs = ["bad", "still bad"]
        self.svc.blocked = lambda *_: False
        await self.refresh()
        await self.refresh()
        self.assertEqual(len(self.ctx.llm_calls), 2)

    async def test_missing_event_id_skips_only_missing_message(self):
        self.ctx.outputs = [json.dumps({"E1": "有消息"}, ensure_ascii=False)]
        self.svc.blocked = lambda *_: False
        await self.refresh()
        self.assertEqual(self.svc.state["entries"][0]["message"], "有消息")
        self.assertEqual(self.svc.state["entries"][1]["message"], "")

    async def test_schedule_change_replaces_old_plan(self):
        self.svc.blocked = lambda *_: False
        await self.refresh()
        old_hash = self.svc.state["schedule_hash"]
        self.ctx.schedule = ROW
        self.ctx.outputs.append(json.dumps({"E1": "新计划"}, ensure_ascii=False))
        await self.refresh()
        self.assertNotEqual(old_hash, self.svc.state["schedule_hash"])
        self.assertEqual(len(self.svc.state["entries"]), 1)

    async def test_expired_due_item_is_marked_not_sent(self):
        from datetime import datetime, timedelta
        self.svc.cfg["blocked_windows"] = []
        now = datetime.now().replace(second=0, microsecond=0)
        trigger = (now - timedelta(minutes=3)).strftime("%H:%M")
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": trigger, "message": "过期", "sent": False}]
        await self.svc.send_due(now)
        self.assertTrue(self.svc.state["entries"][0]["expired"])
        self.assertFalse(self.ctx.sent)
        await self.svc.send_due(now)
        self.assertFalse(self.ctx.sent)

    async def test_sent_entry_not_sent_twice(self):
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": "08:55", "message": "准备上课", "sent": True}]
        await self.svc.send_due()
        self.assertEqual(self.ctx.sent, [])

    async def test_expired_entry_no_catchup(self):
        from datetime import datetime, timedelta
        past = datetime.now() - timedelta(minutes=3)
        entry = {"trigger_time": past.strftime("%H:%M"), "message": "过期", "sent": False}
        self.assertTrue(self.svc._expired(entry, datetime.now()))

    async def test_dry_run_never_sends(self):
        from datetime import datetime
        import astrbot.api
        astrbot.api.logger.info = lambda *_args, **_kwargs: None
        now = datetime.now().replace(second=0, microsecond=0)
        self.svc.cfg.update(dry_run=True, blocked_windows=[])
        self.svc.state["entries"] = [{"id": "E1", "trigger_time": now.strftime("%H:%M"), "message": "预览", "sent": False}]
        await self.svc.send_due(now)
        self.assertEqual(self.ctx.sent, [])
        self.assertFalse(self.svc.state["entries"][0]["sent"])
        self.assertTrue(self.svc.state["entries"][0]["dry_run_logged"])

    async def test_terminate_cancels_worker(self):
        async def idle():
            await asyncio.Event().wait()
        self.svc.run = idle
        self.svc.start()
        await asyncio.sleep(0)
        await self.svc.stop()
        self.assertIsNone(self.svc._task)


if __name__ == "__main__":
    unittest.main()
