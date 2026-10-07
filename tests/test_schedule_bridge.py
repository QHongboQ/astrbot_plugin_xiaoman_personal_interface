from __future__ import annotations
import json, tempfile, types, unittest
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
    def _cfg(self, key, default=None): return self.config.get(key, default)
    def _periods(self): return [("09:00", "18:00")]
    def _weekdays(self): return list(range(7))
    def _provider_affected(self, provider_id): return True
    @staticmethod
    def _is_peak(local, periods, weekdays): return local.hour >= 9 and local.hour < 18

class Fixture:
    def __init__(self, kind="holiday", fish=None):
        self.day = Day(kind); self.fish = fish or Fish(); self.stars = []
    def get_all_stars(self): return self.stars

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
    def setup_bridge(self, kind="holiday", override="auto", native=None, enabled=True):
        f=Fixture(kind, Fish(override, enabled));
        if native is not None: f.fish.get_wallet_policy=native
        f.stars=[meta(FF,f.fish),meta(TA,types.SimpleNamespace())]
        bridge=FatFishBridge(f,f.day); bridge.install(); return f,bridge
    def test_fatfish_exact_discovery(self):
        f,b=self.setup_bridge(); f.stars.insert(0,meta(FF+"_copy",object())); self.assertIs(b.discover(),f.fish)
    def test_native_policy_direct_no_patch(self):
        native=lambda **kw:{"allowed":True,"state":"native"}
        f,b=self.setup_bridge(native=native); self.assertIs(f.fish.get_wallet_policy,native); self.assertTrue(b.get_wallet_policy()["native_policy"])
    def test_holiday_policy_and_virtual_allow_persisted_auto(self):
        from datetime import timezone, timedelta
        at=datetime(2026,10,7,14,55,tzinfo=timezone(timedelta(hours=8)))
        f,b=self.setup_bridge(); p=b.get_wallet_policy(at=at); self.assertEqual((p["state"],p["day_kind"],p["allowed"]),("offpeak","holiday",True)); self.assertEqual(f.fish._cfg("manual_override"),"always_allow"); self.assertEqual(f.fish.config["manual_override"],"auto")
    def test_weekend_virtual_allow(self):
        f,b=self.setup_bridge("weekend"); self.assertTrue(b.get_wallet_policy()["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"always_allow")
    def test_adjusted_workday_uses_peak(self):
        f,b=self.setup_bridge("adjusted"); p=b.get_wallet_policy(at=datetime(2026,10,7,14,55)); self.assertFalse(p["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"auto")
    def test_manual_block_wins_holiday(self):
        f,b=self.setup_bridge("holiday","always_block"); self.assertFalse(b.get_wallet_policy()["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"always_block")
    def test_manual_allow_wins(self): self.assertTrue(self.setup_bridge("adjusted","always_allow")[1].get_wallet_policy()["allowed"])
    def test_disabled_allows(self):
        p=self.setup_bridge("adjusted",enabled=False)[1].get_wallet_policy(); self.assertTrue(p["allowed"]); self.assertEqual(p["state"],"disabled")
    def test_unknown_preserves_auto_and_normal_peak(self):
        f,b=self.setup_bridge("unknown"); p=b.get_wallet_policy(at=datetime(2026,10,7,14,55)); self.assertFalse(p["allowed"]); self.assertEqual(f.fish._cfg("manual_override"),"auto")
    def test_only_manual_override_intercepted(self):
        f,b=self.setup_bridge(); self.assertEqual(f.fish._cfg("anything","x"),"x")
    def test_double_install_and_restore(self):
        f,b=self.setup_bridge(); wrapper=f.fish._cfg; b.install(); self.assertIs(f.fish._cfg,wrapper); b.uninstall(); self.assertEqual(f.fish._cfg("manual_override"),"auto"); self.assertFalse(hasattr(f.fish,"get_wallet_policy"))
    def test_later_patch_not_overwritten(self):
        f,b=self.setup_bridge(); replacement=lambda key,default=None:"later"; f.fish._cfg=replacement; b.uninstall(); self.assertIs(f.fish._cfg,replacement)
    def test_added_policy_cleanup(self):
        f,b=self.setup_bridge(); self.assertTrue(callable(f.fish.get_wallet_policy)); b.uninstall(); self.assertFalse(hasattr(f.fish,"get_wallet_policy"))

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
    async def test_static_fallback_uses_timeawareness_summary_api(self):
        plugin=self.ctx.stars[0].star_cls
        plugin.daily_schedule_service.register_session_async=lambda *a,**k: __import__("asyncio").sleep(0,result="")
        plugin.daily_schedule_service.today_schedule_summary=lambda session,*,now:"08:00-09:00 早读 | 10:00-11:00 上课"
        result=await TimeAwarenessAdapter(self.ctx).get_daily_schedule("umo",at=datetime(2026,10,7),allow_generate=False)
        self.assertEqual([s["name"] for s in result["slots"]],["早读","上课"])
        self.assertEqual(result["slots"][0]["origin"],"static")

class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now=datetime(2026,10,7,18,30,tzinfo=__import__("datetime").timezone.utc)
        self.slots=[]
        self.calls=[]; self.reg_calls=[]; self.sent=[]; self.denied=set()
        class ScheduleService:
            async def register_session_async(_s, session, *, trigger=True): self.reg_calls.append((session,trigger)); return "persona"
            def get_snapshot_for_session(_s, session, *, now): return {"persona_hash":"persona","snapshot_id":"sid","local_date":now.date().isoformat(),"timezone":"UTC","generated_at":"g","manually_edited":False}
        class Admin:
            def get_detail(_s,*args,**kwargs): return {"slots":self.slots}
        ta=types.SimpleNamespace(daily_schedule_service=ScheduleService(),daily_schedule_admin=Admin(),time_context=types.SimpleNamespace(now=lambda:self.now))
        class NativeFish:
            def get_wallet_policy(_s, *, at=None, provider_id=None):
                at=at or self.now
                return {"enabled":True,"allowed":at.strftime("%H:%M") not in self.denied,"state":"peak" if at.strftime("%H:%M") in self.denied else "offpeak","evaluated_at":at,"timezone":"UTC","manual_override":"auto","provider_affected":True,"day_kind":"holiday","day_label":"holiday"}
        self.fish=NativeFish()
        async def conversations(): return [types.SimpleNamespace(user_id="qq:GroupMessage:g1"),types.SimpleNamespace(user_id="qq:FriendMessage:u1")]
        async def persona(): return {"prompt":"persona unchanged"}
        async def provider(umo): return "provider"
        async def llm(**kwargs): self.calls.append(kwargs); return types.SimpleNamespace(completion_text=json.dumps({f"E{i}":f"message{i}" for i in range(1,10)}))
        async def send(umo,chain): self.sent.append((umo,chain))
        self.ctx=types.SimpleNamespace(get_all_stars=lambda:[meta(TA,ta),meta(FF,self.fish)],
            conversation_manager=types.SimpleNamespace(get_conversations=conversations),
            persona_manager=types.SimpleNamespace(get_default_persona_v3=persona),
            get_current_chat_provider_id=provider,llm_generate=llm,send_message=send)
        self.service=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True}},".")
        self.saved=[]
        self.service._save=lambda:self.saved.append(json.loads(json.dumps(self.service.state)))

    async def test_config_fallback_and_schema_no_block_windows(self):
        schema=json.loads((Path(__file__).parents[1]/"_conf_schema.json").read_text(encoding="utf-8")); self.assertIn("schedule_source_umo",schema["schedule_broadcast"]["items"]); self.assertNotIn("blocked_windows",str(schema))
    async def test_schedule_change_digest_and_empty_slots_skipped(self):
        from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.time_awareness_adapter import TimeAwarenessAdapter
        self.slots=[{"start":"20:00","end":"21:00","name":"","state":""},{"start":"22:00","end":"23:00","name":"reading","state":"read"}]
        adapter=TimeAwarenessAdapter(self.ctx); self.assertIsNotNone(adapter.discover()); self.assertIsNotNone(await adapter.get_daily_schedule("qq:GroupMessage:g1",at=self.now))
        self.assertTrue(await self.service.refresh(now=self.now),self.service.last_error); self.assertEqual(len(self.calls),1)
        self.assertIn("persona unchanged",self.calls[0]["system_prompt"])
        self.assertEqual(len(self.service.state["entries"]),1)
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
        self.assertTrue(self.service.state["entries"][0]["sent"])
        self.assertEqual(len(self.sent),2)
        reloaded=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True}},".")
        reloaded.state=self.saved[-1]
        self.assertEqual(reloaded.state["entries"][0]["delivered_umos"],["qq:FriendMessage:u1","qq:GroupMessage:g1"])
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
        entry=self.service.state["entries"][0]; self.assertFalse(entry["sent"]); self.assertEqual(entry["delivered_umos"],["qq:FriendMessage:u1"])
        failed["on"]=False; await self.service.send_due(now)
        self.assertTrue(entry["sent"]); self.assertEqual(attempted[2:],["qq:GroupMessage:g1"])
    async def test_zero_targets_and_expiration_do_not_mark_sent_or_catch_up(self):
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        async def no_targets(): return []
        self.ctx.conversation_manager.get_conversations=no_targets
        now=self.now.replace(hour=20,minute=30); await self.service.send_due(now)
        entry=self.service.state["entries"][0]; self.assertFalse(entry["sent"])
        await self.service.send_due(now.replace(minute=32)); self.assertTrue(entry["expired"]); self.assertFalse(self.sent)
    async def test_dry_run_and_live_policy_block_prevent_sends(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        self.service.cfg["dry_run"]=True
        await self.service.send_due(self.now.replace(hour=20,minute=30)); self.assertFalse(self.sent)
        self.service.cfg["dry_run"]=False
        self.service.state["entries"][0]["dry_run_logged"]=False
        self.denied.add("20:30"); await self.service.send_due(self.now.replace(hour=20,minute=30)); self.assertFalse(self.sent)
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
