from __future__ import annotations
import copy, json, tempfile, types, unittest
from datetime import datetime
from pathlib import Path

from test_photo_tool import MAIN_MODULE
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.time_awareness_adapter import TimeAwarenessAdapter
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.fat_fish_bridge import FatFishBridge
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.schedule_broadcast import ScheduleBroadcastService

TA, FF = "time_awareness", "astrbot_plugin_fat_fish_wallet"

class Day:
    def __init__(self, kind="holiday"):
        self.kind = kind
    def get_day_policy(self, at=None):
        return {"available": self.kind != "unknown", "kind": self.kind, "label": "假日" if self.kind == "holiday" else "", "evaluated_at": at}

class Fish:
    def __init__(self, override="auto", enabled=True):
        self.config = {"manual_override": override, "enabled": enabled}
        self.provider_calls = []
        self.unknown_provider_affected = True
    def _cfg(self, key, default=None): return self.config.get(key, default)
    def _periods(self): return [types.SimpleNamespace(start=9 * 3600, end=18 * 3600)]
    def _weekdays(self): return list(range(7))
    def _provider_affected(self, provider_id, prov):
        self.provider_calls.append((provider_id, prov))
        return self.unknown_provider_affected if prov is None else True
    @staticmethod
    def _is_peak(local, periods, weekdays): return local.hour >= 9 and local.hour < 18

class Fixture:
    def __init__(self, kind="holiday", fish=None):
        self.day = Day(kind); self.fish = fish or Fish(); self.stars = []
        self.providers = {}; self.provider_lookups = []; self.provider_lookup_error = False
    def get_all_stars(self): return self.stars
    def get_provider_by_id(self, provider_id):
        self.provider_lookups.append(provider_id)
        if self.provider_lookup_error: raise RuntimeError("provider lookup unavailable")
        return self.providers.get(provider_id)

def meta(name, instance, active=True, version="v2.3.0"):
    return types.SimpleNamespace(name=name, star_cls=instance, activated=active, version=version, config={})

class TimeAwarenessTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_discovery(self):
        f=Fixture(); other=object(); good=object(); f.stars=[meta("my_time_awareness",other),meta(TA,good,False),meta(TA,good)]
        self.assertIs(TimeAwarenessAdapter(f).discover(), good)
    def test_missing_discovery(self): self.assertIsNone(TimeAwarenessAdapter(Fixture()).discover())
    def test_workday_normalization(self):
        f=Fixture(); f.stars=[meta(TA,types.SimpleNamespace(time_context=types.SimpleNamespace(facts=types.SimpleNamespace(collect=lambda **kw: types.SimpleNamespace(workday=types.SimpleNamespace(kind="adjusted",available=True,value="调休上班"),now=kw["now"]))))) ]
        policy=TimeAwarenessAdapter(f).get_day_policy(datetime(2026,10,7))
        self.assertEqual(policy["kind"],"adjusted"); self.assertTrue(policy["available"])

class BridgeTests(unittest.TestCase):
    def setup_bridge(self, kind="holiday", override="auto", enabled=True):
        f=Fixture(kind, Fish(override, enabled));
        f.stars=[meta(FF,f.fish),meta(TA,types.SimpleNamespace())]
        bridge=FatFishBridge(f,f.day); bridge.install(); return f,bridge
    def test_fatfish_exact_discovery(self):
        f,b=self.setup_bridge(); f.stars.insert(0,meta(FF+"_copy",object())); self.assertIs(b.discover(),f.fish)
    def test_holiday_policy_and_virtual_allow_persisted_auto(self):
        from datetime import timezone, timedelta
        at=datetime(2026,10,7,14,55,tzinfo=timezone(timedelta(hours=8)))
        f,b=self.setup_bridge(); p=b.get_wallet_policy(at=at); self.assertEqual((p["state"],p["day_kind"],p["allowed"]),("offpeak","holiday",True)); self.assertEqual(f.fish._cfg("manual_override"),"always_allow"); self.assertEqual(f.fish.config["manual_override"],"auto")
    def test_weekend_virtual_allow(self):
        f,b=self.setup_bridge("weekend"); self.assertTrue(b.get_wallet_policy()["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"always_allow")
    def test_adjusted_workday_uses_peak(self):
        from datetime import timezone, timedelta
        f,b=self.setup_bridge("adjusted"); at=datetime(2026,10,7,14,55,tzinfo=timezone(timedelta(hours=8))); p=b.get_wallet_policy(at=at); self.assertFalse(p["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"auto")
    def test_manual_block_wins_holiday(self):
        f,b=self.setup_bridge("holiday","always_block"); self.assertFalse(b.get_wallet_policy()["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"always_block")
    def test_manual_allow_wins(self): self.assertTrue(self.setup_bridge("adjusted","always_allow")[1].get_wallet_policy()["allowed"])
    def test_disabled_allows(self):
        p=self.setup_bridge("adjusted",enabled=False)[1].get_wallet_policy(); self.assertTrue(p["allowed"]); self.assertEqual(p["state"],"disabled")
    def test_unknown_preserves_auto_and_normal_peak(self):
        f,b=self.setup_bridge("unknown"); p=b.get_wallet_policy(at=datetime(2026,10,7,14,55)); self.assertFalse(p["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"auto")
    def test_only_manual_override_intercepted(self):
        f,b=self.setup_bridge(); self.assertEqual(f.fish._cfg("anything","x"),"x")
    def test_provider_object_is_resolved_and_passed_to_fatfish(self):
        f,b=self.setup_bridge("workday"); provider=object(); f.providers["deepseek"]=provider
        p=b.get_wallet_policy(at=datetime(2026,10,7,12,30),provider_id="deepseek")
        self.assertEqual(f.provider_lookups,["deepseek"])
        self.assertEqual(f.fish.provider_calls[-1],("deepseek",provider))
        self.assertTrue(p["provider_affected"])
    def test_missing_or_failed_provider_lookup_passes_none_to_fatfish(self):
        for lookup_error in (False,True):
            with self.subTest(lookup_error=lookup_error):
                f,b=self.setup_bridge("workday"); f.provider_lookup_error=lookup_error
                f.fish.unknown_provider_affected=False
                p=b.get_wallet_policy(at=datetime(2026,10,7,12,30),provider_id="deepseek")
                self.assertEqual(f.fish.provider_calls[-1],("deepseek",None))
                self.assertFalse(p["provider_affected"])
    def test_double_install_and_restore(self):
        f,b=self.setup_bridge(); wrapper=f.fish._cfg; b.install(); self.assertIs(f.fish._cfg,wrapper); b.uninstall(); self.assertEqual(f.fish._cfg("manual_override"),"auto")
    def test_later_patch_not_overwritten(self):
        f,b=self.setup_bridge(); replacement=lambda key,default=None:"later"; f.fish._cfg=replacement; b.uninstall(); self.assertIs(f.fish._cfg,replacement)

class AdapterScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[]
        class Svc:
            async def register_session_async(_self,session,*,trigger=True): self.calls.append((session,trigger)); return "ph"
            def get_snapshot_for_session(_self,session,*,now): return {"persona_hash":"ph","snapshot_id":"snap","local_date":now.date().isoformat(),"timezone":"UTC","generated_at":"g","manually_edited":False}
        class Admin:
            def get_detail(_self,*args,**kwargs): return {"slots":[{"slot_ref":"ref","start":"20:00","end":"21:00","name":"x","state":"y","origin":"user","source_origin":"ai"}]}
        plugin=types.SimpleNamespace(daily_schedule_service=Svc(),daily_schedule_admin=Admin(),time_context=types.SimpleNamespace(now=lambda:datetime(2026,10,7)))
        self.ctx=Fixture(); self.ctx.stars=[meta(TA,plugin)]
    async def test_existing_snapshot_no_generate_effective_merge(self):
        result=await TimeAwarenessAdapter(self.ctx).get_daily_schedule("umo",at=datetime(2026,10,7),allow_generate=False)
        self.assertEqual(self.calls,[("umo",False)])
        self.assertEqual(result["source"],"time_awareness"); self.assertEqual(result["slots"][0]["source_origin"],"ai")
    async def test_explicit_generate_request_denied(self): self.assertIsNone(await TimeAwarenessAdapter(self.ctx).get_daily_schedule("umo",allow_generate=True))
    async def test_missing_snapshot_returns_unavailable_without_generation(self):
        plugin=self.ctx.stars[0].star_cls
        plugin.daily_schedule_service.register_session_async=lambda *a,**k: __import__("asyncio").sleep(0,result="")
        plugin.daily_schedule_service.today_schedule_summary=lambda session,*,now:"08:00-09:00 早读 | 10:00-11:00 上课"
        result=await TimeAwarenessAdapter(self.ctx).get_daily_schedule("umo",at=datetime(2026,10,7),allow_generate=False)
        self.assertIsNone(result)

class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from datetime import timezone, timedelta
        self.now=datetime(2026,10,7,18,30,tzinfo=timezone(timedelta(hours=8)))
        self.slots=[]
        self.calls=[]; self.reg_calls=[]; self.sent=[]; self.denied=set()
        self.available_dates={self.now.date()}; self.day_kinds={self.now.date():"holiday"}
        class ScheduleService:
            async def register_session_async(_s, session, *, trigger=True): self.reg_calls.append((session,trigger)); return "persona"
            def get_snapshot_for_session(_s, session, *, now):
                return {"persona_hash":"persona","snapshot_id":"sid-"+now.date().isoformat(),"local_date":now.date().isoformat(),"timezone":"Asia/Shanghai","generated_at":"g","manually_edited":False} if now.date() in self.available_dates else None
        class Admin:
            def get_detail(_s,*args,**kwargs): return {"slots":self.slots}
        def collect(*,scope,now): return types.SimpleNamespace(workday=types.SimpleNamespace(kind=self.day_kinds.get(now.date(),"unknown"),available=now.date() in self.day_kinds,value=""),now=now)
        ta=types.SimpleNamespace(daily_schedule_service=ScheduleService(),daily_schedule_admin=Admin(),time_context=types.SimpleNamespace(now=lambda:self.now,facts=types.SimpleNamespace(collect=collect)))
        class NativeFish:
            def _periods(_s): return [types.SimpleNamespace(start=9*3600,end=12*3600),types.SimpleNamespace(start=14*3600,end=18*3600)]
            def get_wallet_policy(_s, *, at=None, provider_id=None):
                at=at or self.now
                return {"enabled":True,"allowed":at.strftime("%H:%M") not in self.denied,"state":"peak" if at.strftime("%H:%M") in self.denied else "offpeak","evaluated_at":at,"timezone":"UTC","manual_override":"auto","provider_affected":True,"day_kind":"holiday","day_label":"holiday"}
        self.fish=NativeFish()
        async def conversations(): return [types.SimpleNamespace(user_id="qq:GroupMessage:g1"),types.SimpleNamespace(user_id="qq:FriendMessage:u1")]
        async def persona(): return {"prompt":"persona unchanged"}
        self.provider_lookups=[]
        async def provider(umo): self.provider_lookups.append(umo); return "provider"
        async def llm(**kwargs):
            import re
            self.calls.append(kwargs)
            ids=re.findall(r'"id":\s*"([^"]+)"',kwargs["prompt"])
            return types.SimpleNamespace(completion_text=json.dumps({key:f"message-{key}" for key in ids}))
        async def send(umo,chain): self.sent.append((umo,chain))
        self.ctx=types.SimpleNamespace(get_all_stars=lambda:[meta(TA,ta),meta(FF,self.fish)],
            conversation_manager=types.SimpleNamespace(get_conversations=conversations),
            persona_manager=types.SimpleNamespace(get_default_persona_v3=persona),
            get_current_chat_provider_id=provider,llm_generate=llm,send_message=send)
        class Policy:
            def __init__(_self): _self.calls=[]
            def discover(_self): return self.fish
            def get_wallet_policy(_self, *, at=None, provider_id=None):
                _self.calls.append((at,provider_id))
                at=at or self.now
                local=at
                kind=self.day_kinds.get(local.date(),"unknown")
                peak=kind in {"workday","adjusted"} and ((9 <= local.hour < 12) or (14 <= local.hour < 18))
                return {"found":True,"enabled":True,"allowed":at.strftime("%H:%M") not in self.denied,
                        "state":"peak" if peak else "offpeak",
                        "evaluated_at":at,"timezone":"Asia/Shanghai","manual_override":"auto",
                        "provider_affected":True,"day_kind":kind,"day_label":kind}
        self.policy=Policy()
        self.service=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True,"provider_id":"provider"}},".",fat_fish=self.policy)
        self.saved=[]
        self.service._save=lambda:self.saved.append(json.loads(json.dumps(self.service.state)))

    async def _prepare_simulation(self, slots):
        from datetime import timezone, timedelta
        self.now=datetime(2026,10,7,4,0,tzinfo=timezone(timedelta(hours=8)))
        self.available_dates={self.now.date()}
        self.day_kinds={self.now.date():"holiday"}
        self.slots=slots
        self.service.cfg["dry_run"]=False
        self.assertTrue(await self.service.refresh(now=self.now),self.service.last_error)
        return copy.deepcopy(self.service.plan_for_date(self.now.date()))

    async def test_simulate_due_event_sends_only_due_and_preserves_formal_plan(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
            {"start":"10:30","end":"10:40","name":"future","state":"y"},
        ])
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual(result["hit_event_ids"],[original["entries"][0]["id"]])
        self.assertEqual((result["target_count"],result["success_count"],result["failure_count"]),(2,2,0))
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_early_time_does_not_send(self):
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"future","state":"x"},
        ])
        result,error=await self.service.simulate_time("09:59")
        self.assertFalse(error)
        self.assertEqual(result["hit_event_ids"],[])
        self.assertEqual(result["success_count"],0)
        self.assertEqual(len(self.sent),0)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_expired_time_does_not_send_or_expire_formal_entry(self):
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"expired","state":"x"},
        ])
        result,error=await self.service.simulate_time("10:02")
        self.assertFalse(error)
        self.assertEqual(result["hit_event_ids"],[])
        self.assertEqual(result["expired_event_ids"],[original["entries"][0]["id"]])
        self.assertIn("超过宽限期",result["reason"])
        self.assertEqual(len(self.sent),0)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_with_no_targets_reports_reason_without_mutation(self):
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
        ])
        async def no_targets(): return []
        self.ctx.conversation_manager.get_conversations=no_targets
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual(result["target_count"],0)
        self.assertEqual((result["success_count"],result["failure_count"]),(0,0))
        self.assertIn("没有符合",result["reason"])
        self.assertEqual(len(self.sent),0)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_obeys_chat_type_allowlist_and_denylist(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
        ])
        self.service.cfg.update({
            "send_groups":True, "send_private":False,
            "allowlist_umos":["qq:GroupMessage:g1","qq:FriendMessage:u1"],
            "denylist_umos":["qq:FriendMessage:u1"],
        })
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual((result["target_count"],result["success_count"],result["failure_count"]),(1,1,0))
        self.assertEqual([umo for umo,_ in self.sent],["qq:GroupMessage:g1"])
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_partial_send_failure_reports_target_and_reason(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
        ])
        async def flaky(umo,chain):
            if umo.endswith(":g1"):
                raise RuntimeError("group temporarily unavailable")
            self.sent.append((umo,chain))
        self.ctx.send_message=flaky
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual((result["success_count"],result["failure_count"]),(1,1))
        self.assertEqual(result["failures"][0]["umo"],"qq:GroupMessage:g1")
        self.assertIn("temporarily unavailable",result["failures"][0]["reason"])
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_simulate_can_send_under_persisted_dry_run_without_changing_it(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
        ])
        self.service.cfg["dry_run"]=True
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual(result["success_count"],2)
        self.assertTrue(self.service.cfg["dry_run"])
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_repeated_simulation_uses_fresh_isolated_state(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        original=await self._prepare_simulation([
            {"start":"10:00","end":"10:10","name":"due","state":"x"},
        ])
        first,error=await self.service.simulate_time("10:00")
        second,error2=await self.service.simulate_time("10:00")
        self.assertFalse(error); self.assertFalse(error2)
        self.assertEqual(first["hit_event_ids"],second["hit_event_ids"])
        self.assertEqual(len(self.sent),4)
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_config_schema_has_no_block_windows(self):
        schema=json.loads((Path(__file__).parents[1]/"_conf_schema.json").read_text(encoding="utf-8")); self.assertIn("schedule_source_umo",schema["schedule_broadcast"]["items"]); self.assertNotIn("blocked_windows",str(schema))
    async def test_schedule_change_digest_and_empty_slots_skipped(self):
        from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.time_awareness_adapter import TimeAwarenessAdapter
        self.slots=[{"start":"20:00","end":"21:00","name":"","state":""},{"start":"22:00","end":"23:00","name":"reading","state":"read"}]
        adapter=TimeAwarenessAdapter(self.ctx); self.assertIsNotNone(adapter.discover()); self.assertIsNotNone(await adapter.get_daily_schedule("qq:GroupMessage:g1",at=self.now))
        self.assertTrue(await self.service.refresh(now=self.now),self.service.last_error); self.assertEqual(len(self.calls),1)
        self.assertIn("persona unchanged",self.calls[0]["system_prompt"])
        entries=self.service.plan_for_date(self.now.date())["entries"]
        self.assertEqual(len(entries),1)
        self.assertFalse(await self.service.refresh(now=self.now)); self.assertEqual(len(self.calls),1)
        self.slots[0]["state"]="new"; self.calls.clear()
        self.assertTrue(await self.service.refresh(now=self.now)); self.assertEqual(len(self.calls),1)
    async def test_past_and_blocked_slots_filtered(self):
        self.slots=[{"start":"07:30","name":"past","state":"x"},{"start":"12:10","name":"blocked","state":"x"},{"start":"20:30","name":"future","state":"x"}]
        self.denied.add("12:10")
        await self.service.refresh(now=self.now)
        prompt=self.calls[0]["prompt"]
        self.assertNotIn("past",prompt); self.assertNotIn("blocked",prompt); self.assertIn("future",prompt)
    async def test_batch_once_zero_send_llm_retry_persistence_dry_run_contracts(self):
        import astrbot.api.event
        class MessageChain:
            def message(self, value): self.value=value; return self
        astrbot.api.event.MessageChain=MessageChain
        now=self.now.replace(hour=20,minute=30)
        self.slots=[{"start":"20:30","name":"future","state":"state"}]
        await self.service.refresh(now=self.now)
        self.assertEqual(len(self.calls),1)
        await self.service.send_due(now)
        self.assertEqual(len(self.calls),1)
        entries=self.service.plan_for_date(self.now.date())["entries"]
        self.assertTrue(entries[0]["sent"])
        self.assertEqual(len(self.sent),2)
        reloaded=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True}},".")
        reloaded.state=self.saved[-1]
        self.assertEqual(reloaded.state["plans"][self.now.date().isoformat()]["entries"][0]["delivered_umos"],["qq:FriendMessage:u1","qq:GroupMessage:g1"])
    async def test_partial_delivery_retries_only_failed_target(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): self.value=value; return self
        astrbot.api.event.MessageChain=Chain
        now=self.now.replace(hour=20,minute=30); self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        failed={"on":True}; attempted=[]
        async def flaky(umo,chain):
            attempted.append(umo)
            if umo.endswith(":g1") and failed["on"]: raise RuntimeError("offline")
            self.sent.append((umo,chain))
        self.ctx.send_message=flaky
        await self.service.send_due(now)
        entry=self.service.plan_for_date(self.now.date())["entries"][0]; self.assertFalse(entry["sent"]); self.assertEqual(entry["delivered_umos"],["qq:FriendMessage:u1"])
        failed["on"]=False; await self.service.send_due(now)
        self.assertTrue(entry["sent"]); self.assertEqual(attempted[2:],["qq:GroupMessage:g1"])
    async def test_zero_targets_and_expiration_do_not_mark_sent_or_catch_up(self):
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        async def no_targets(): return []
        self.ctx.conversation_manager.get_conversations=no_targets
        now=self.now.replace(hour=20,minute=30); await self.service.send_due(now)
        entry=self.service.plan_for_date(self.now.date())["entries"][0]; self.assertFalse(entry["sent"])
        await self.service.send_due(now.replace(minute=32)); self.assertTrue(entry["expired"]); self.assertFalse(self.sent)

    async def test_effective_peak_cover_plan_and_next_day_snapshot_at_0400(self):
        from datetime import timezone, timedelta
        now=datetime(2026,10,8,4,0,tzinfo=timezone(timedelta(hours=8)))
        target=now.date()+timedelta(days=1)
        self.now=now
        self.available_dates={target}
        self.day_kinds={target:"workday"}
        self.slots=[
            {"start":"08:30","end":"08:45","name":"早餐","state":"吃饭"},
            {"start":"09:20","end":"09:40","name":"课程","state":"学习"},
            {"start":"10:30","end":"10:45","name":"咖啡","state":"休息"},
            {"start":"11:30","end":"11:45","name":"购物","state":"外出"},
            {"start":"12:20","end":"12:50","name":"午餐","state":"吃饭"},
            {"start":"13:30","end":"13:45","name":"散步","state":"外出"},
            {"start":"14:20","end":"14:40","name":"拍摄","state":"工作"},
            {"start":"15:30","end":"15:45","name":"茶歇","state":"休息"},
            {"start":"17:30","end":"17:45","name":"收尾","state":"工作"},
            {"start":"18:30","end":"19:00","name":"晚饭","state":"吃饭"},
        ]
        self.service.state["plans"][now.date().isoformat()]={"local_date":now.date().isoformat(),"entries":[{"id":"today-remains"}]}
        self.assertTrue(await self.service.refresh(now=now),self.service.last_error)
        plan=self.service.plan_for_date(target)
        self.assertIsNotNone(plan)
        self.assertEqual(self.service.plan_for_date(now.date())["entries"][0]["id"],"today-remains")
        entries=plan["entries"]
        starts=[e for e in entries if e["kind"]=="PEAK_START"]
        ends=[e for e in entries if e["kind"]=="PEAK_END"]
        normal=[e for e in entries if e["kind"]=="NORMAL"]
        self.assertEqual((len(starts),len(ends)),(2,2))
        self.assertTrue(all(datetime.fromisoformat(e["trigger_at"]).tzinfo is not None for e in entries))
        start_times=[datetime.fromisoformat(e["trigger_at"]) for e in starts]
        end_times=[datetime.fromisoformat(e["trigger_at"]) for e in ends]
        self.assertTrue(all((t.hour,t.minute) in {(8,55),(8,56),(8,57),(8,58),(8,59),(13,55),(13,56),(13,57),(13,58),(13,59)} for t in start_times))
        self.assertTrue(all((t.hour,t.minute) in {(12,0),(12,1),(12,2),(12,3),(12,4),(12,5),(18,0),(18,1),(18,2),(18,3),(18,4),(18,5)} for t in end_times))
        normal_names={e["name"] for e in normal}
        self.assertEqual(normal_names,{"早餐","午餐","散步","晚饭"})
        self.assertEqual(len(self.calls),1)
        self.assertTrue(plan["plan_complete"])
        self.assertTrue(all(e["message"] for e in entries))
        raw=self.service.last_schedules[target.isoformat()]
        regenerated=self.service._effective_entries(raw,target,"Asia/Shanghai","workday","provider",now)
        self.assertEqual([(e["kind"],e["trigger_at"]) for e in entries],
                         [(e["kind"],e["trigger_at"]) for e in regenerated])
        self.assertEqual(len(self.reg_calls),2)
        self.assertTrue(all(trigger is False for _,trigger in self.reg_calls))
        periods=self.fish._periods()
        self.assertTrue(all(not isinstance(period,(tuple,list)) for period in periods))
        windows=self.service._peak_windows(target,"Asia/Shanghai","workday","provider")
        self.assertEqual(len(windows),2)

    async def test_holiday_weekend_suppress_no_raw_events_and_adjusted_uses_peaks(self):
        from datetime import timedelta
        target=self.now.date()+timedelta(days=1)
        self.available_dates={target}
        self.slots=[{"start":"09:30","end":"10:00","name":"peak slot","state":"busy"},
                    {"start":"13:00","end":"13:20","name":"outside","state":"free"}]
        for kind, expected_peak_entries in (("holiday",0),("weekend",0),("adjusted",4)):
            with self.subTest(kind=kind):
                self.day_kinds={target:kind}
                self.service.state={"plans":{}}
                self.calls.clear()
                result,message=await self.service.build_date(target,now=self.now)
                self.assertTrue(result,message)
                plan=self.service.plan_for_date(target)
                peak=[e for e in plan["entries"] if e["kind"].startswith("PEAK_")]
                self.assertEqual(len(peak),expected_peak_entries)
                normal=[e for e in plan["entries"] if e["kind"]=="NORMAL"]
                self.assertEqual({e["name"] for e in normal},{"peak slot","outside"} if not expected_peak_entries else {"outside"})
                self.assertEqual(len(self.calls),1)
    async def test_saved_message_sends_without_send_time_fatfish_or_llm_calls(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        self.service.cfg["provider_id"]=""
        await self.service.refresh(now=self.now)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.provider_lookups),1)
        self.service.cfg["dry_run"]=True
        policy_calls=len(self.policy.calls)
        await self.service.send_due(self.now.replace(hour=20,minute=30)); self.assertFalse(self.sent)
        self.assertEqual(len(self.policy.calls),policy_calls)
        self.service.cfg["dry_run"]=False
        self.denied.add("20:30")
        await self.service.send_due(self.now.replace(hour=20,minute=30))
        self.assertEqual(len(self.sent),2)
        self.assertEqual(len(self.policy.calls),policy_calls)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.provider_lookups),1)
    async def test_missing_timeawareness_prevents_generation(self):
        self.ctx.get_all_stars=lambda:[meta(FF,self.fish)]
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now); await self.service.send_due(self.now.replace(hour=20,minute=30))
        self.assertFalse(self.calls); self.assertFalse(self.sent)
    async def test_no_life_scheduler_runtime_dependency(self):
        root=Path(__file__).parents[1]
        source="\n".join(p.read_text(encoding="utf-8") for folder in (root/"services",root/"tools") for p in folder.glob("*.py"))+(root/"main.py").read_text(encoding="utf-8")
        self.assertNotIn("astrbot_plugin_life_scheduler",source); self.assertNotIn("get_life_context",source)

if __name__ == "__main__": unittest.main()
