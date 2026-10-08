from __future__ import annotations
import copy, json, tempfile, types, unittest
from datetime import datetime
from pathlib import Path

from test_photo_tool import MAIN_MODULE
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.time_awareness_adapter import TimeAwarenessAdapter
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.fat_fish_bridge import FatFishBridge
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.rolling_day_bridge import RollingDayBridge
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
        self.provider_is_affected = True
    def _cfg(self, key, default=None): return self.config.get(key, default)
    def _periods(self): return [types.SimpleNamespace(start=9 * 3600, end=18 * 3600)]
    def _weekdays(self): return list(range(7))
    def _provider_affected(self, provider_id, prov):
        self.provider_calls.append((provider_id, prov))
        return self.unknown_provider_affected if prov is None else self.provider_is_affected
    @staticmethod
    def _is_peak(local, periods, weekdays): return local.weekday() in weekdays and 9 <= local.hour < 18

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

class RollingDayBridgeTests(unittest.TestCase):
    def _fixture(self, generation_time="-04:00"):
        config={"generation_time":generation_time}
        class Generation:
            def _build_prompt(_self, **kwargs): return "PLAN"
            def _build_boundary_prompt(_self, **kwargs): return "BOUNDARY"
        class Service:
            def __init__(_self): _self.generation=Generation()
            def _daily_config(_self): return config
            @staticmethod
            def _parse_generation_time(value):
                raw=str(value or "00:05").strip()
                offset=1 if raw.startswith("-") else 0
                raw=raw[1:] if offset else raw
                hour,minute=(int(part) for part in raw.split(":"))
                return hour,minute,offset
        plugin=types.SimpleNamespace(daily_schedule_service=Service())
        ctx=Fixture(); ctx.stars=[meta(TA,plugin)]
        return ctx,plugin,config

    def test_prompt_bridge_reads_boundary_dynamically_and_restores(self):
        ctx,plugin,config=self._fixture()
        generation=plugin.daily_schedule_service.generation
        original_plan=generation._build_prompt
        original_boundary=generation._build_boundary_prompt
        bridge=RollingDayBridge(ctx)
        self.assertTrue(bridge.install())
        plan=generation._build_prompt()
        boundary=generation._build_boundary_prompt()
        self.assertIn("generation_time=-04:00",plan)
        self.assertIn("每天 04:00",plan)
        self.assertIn("00:00-04:00",boundary)
        self.assertIn("04:00-24:00",boundary)
        config["generation_time"]="-03:30"
        self.assertIn("每天 03:30",generation._build_prompt())
        bridge.uninstall()
        self.assertEqual(generation._build_prompt(),"PLAN")
        self.assertEqual(generation._build_boundary_prompt(),"BOUNDARY")
        self.assertEqual(original_plan(),"PLAN")
        self.assertEqual(original_boundary(),"BOUNDARY")

    def test_bridge_fail_open_when_timeawareness_missing(self):
        bridge=RollingDayBridge(Fixture())
        self.assertFalse(bridge.install())
        self.assertFalse(bridge.status()["available"])

    def test_dynamic_peak_prompt_only_for_workdays(self):
        for kind, expected in (("workday", True), ("adjusted", True), ("weekend", False), ("holiday", False)):
            with self.subTest(kind=kind):
                ctx, plugin, _ = self._fixture()
                ctx.day = Day(kind)
                plugin.time_context = types.SimpleNamespace(
                    now=lambda: datetime(2026,10,7,8),
                    facts=types.SimpleNamespace(collect=lambda **kw: types.SimpleNamespace(
                        workday=types.SimpleNamespace(kind=ctx.day.kind, available=True, value=ctx.day.kind),
                        now=kw["now"])))
                fish = Fish()
                fish._periods = lambda: [types.SimpleNamespace(start=9*3600, end=12*3600),
                                         types.SimpleNamespace(start=14*3600, end=18*3600+1800)]
                ctx.stars.append(meta(FF, fish))
                fat_bridge = FatFishBridge(ctx, ctx.day)
                bridge = RollingDayBridge(ctx, TimeAwarenessAdapter(ctx), fat_bridge)
                self.assertTrue(bridge.install())
                prompt = plugin.daily_schedule_service.generation._build_prompt(
                    now=datetime(2026,10,7,8), persona_prompt="", sensors={}, policy=None, enhanced=None, anti_repeat=None)
                self.assertEqual("<XIAOMAN_FAT_FISH_PEAK_BLOCKS>" in prompt, expected)
                boundary = plugin.daily_schedule_service.generation._build_boundary_prompt(now=datetime(2026,10,7,8))
                self.assertNotIn("XIAOMAN_FAT_FISH_PEAK_BLOCKS", boundary)
                if expected:
                    self.assertIn("09:00-12:00、14:00-18:30", prompt)
                    fish._periods = lambda: [types.SimpleNamespace(start=10*3600, end=13*3600)]
                    self.assertIn("10:00-13:00", plugin.daily_schedule_service.generation._build_prompt(
                        now=datetime(2026,10,7,8)))
                bridge.uninstall()

    def test_peak_prompt_uses_effective_fatfish_policy(self):
        from datetime import timezone, timedelta
        for label, day_kind, weekday_set, override, enabled, affected, expected in (
            ("enabled workday", "workday", list(range(7)), "auto", True, True, True),
            ("adjusted weekday", "adjusted", list(range(7)), "auto", True, True, True),
            ("adjusted Saturday excluded by Fat Fish", "adjusted", list(range(5)), "auto", True, True, False),
            ("holiday", "holiday", list(range(7)), "auto", True, True, False),
            ("weekend", "weekend", list(range(7)), "auto", True, True, False),
            ("disabled", "workday", list(range(7)), "auto", False, True, False),
            ("manual allow", "workday", list(range(7)), "always_allow", True, True, False),
            ("manual block", "workday", list(range(7)), "always_block", True, True, False),
            ("provider unaffected", "workday", list(range(7)), "auto", True, False, False),
            ("provider explicitly unaffected", "workday", list(range(7)), "auto", True, True, False),
        ):
            with self.subTest(label=label):
                ctx, plugin, _ = self._fixture()
                ctx.day = Day(day_kind)
                plugin.time_context = types.SimpleNamespace(
                    now=lambda: datetime(2026,10,7,8,tzinfo=timezone(timedelta(hours=8))),
                    facts=types.SimpleNamespace(collect=lambda **kw: types.SimpleNamespace(
                        workday=types.SimpleNamespace(kind=day_kind, available=True, value=day_kind), now=kw["now"])))
                fish=Fish(override, enabled)
                fish._weekdays=lambda:weekday_set
                fish.unknown_provider_affected=affected
                ctx.stars.append(meta(FF,fish))
                adapter=TimeAwarenessAdapter(ctx)
                fat_bridge=FatFishBridge(ctx,adapter); fat_bridge.install()
                provider_id = "deepseek" if label == "provider explicitly unaffected" else ""
                if provider_id:
                    ctx.providers[provider_id]=object()
                    fish.provider_is_affected=False
                bridge=RollingDayBridge(ctx,adapter,fat_bridge,provider_id=provider_id)
                at=datetime(2026,10,10,8,tzinfo=timezone(timedelta(hours=8))) if label == "adjusted Saturday excluded by Fat Fish" else datetime(2026,10,7,8,tzinfo=timezone(timedelta(hours=8)))
                self.assertEqual(bool(bridge._peak_instruction(at)), expected)
                bridge.uninstall(); fat_bridge.uninstall()

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
            def _daily_config(_self): return {"generation_time":"-04:00"}
            @staticmethod
            def _parse_generation_time(value):
                raw=str(value or "00:05").strip()
                offset=1 if raw.startswith("-") else 0
                raw=raw[1:] if offset else raw
                hour,minute=(int(part) for part in raw.split(":"))
                return hour,minute,offset
        class Admin:
            def get_detail(_self,*args,**kwargs): return {"slots":[{"slot_ref":"ref","start":"20:00","end":"21:00","name":"x","state":"y","origin":"user","source_origin":"ai"}]}
        plugin=types.SimpleNamespace(daily_schedule_service=Svc(),daily_schedule_admin=Admin(),time_context=types.SimpleNamespace(now=lambda:datetime(2026,10,7)))
        self.ctx=Fixture(); self.ctx.stars=[meta(TA,plugin)]
    async def test_existing_snapshot_no_generate_effective_merge(self):
        result=await TimeAwarenessAdapter(self.ctx).get_daily_schedule("umo",at=datetime(2026,10,7),allow_generate=False)
        self.assertEqual(self.calls,[("umo",False)])
        self.assertEqual(result["source"],"time_awareness"); self.assertEqual(result["slots"][0]["source_origin"],"ai")
    def test_generation_boundary_reads_timeawareness_config(self):
        adapter=TimeAwarenessAdapter(self.ctx)
        boundary=adapter.get_generation_boundary()
        self.assertEqual((boundary["raw"],boundary["clock"],boundary["target_day_offset"]),("-04:00","04:00",1))

    def test_rolling_day_window_uses_configured_clock_not_hardcoded(self):
        from datetime import timezone, timedelta
        adapter=TimeAwarenessAdapter(self.ctx)
        at=datetime(2026,10,8,2,30,tzinfo=timezone(timedelta(hours=8)))
        window=adapter.rolling_day_window(at)
        self.assertEqual(window["start"].isoformat(),"2026-10-07T04:00:00+08:00")
        self.assertEqual(window["end"].isoformat(),"2026-10-08T04:00:00+08:00")
        plugin=self.ctx.stars[0].star_cls
        plugin.daily_schedule_service._daily_config=lambda:{"generation_time":"-03:30"}
        window=adapter.rolling_day_window(datetime(2026,10,8,6,0,tzinfo=at.tzinfo))
        self.assertEqual(window["clock"],"03:30")
        self.assertEqual(window["start"].isoformat(),"2026-10-08T03:30:00+08:00")

    def test_cycle_dates_follow_rolling_window(self):
        from datetime import timezone, timedelta
        adapter=TimeAwarenessAdapter(self.ctx)
        at=datetime(2026,10,8,6,0,tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual([str(day) for day in adapter.rolling_day_dates(at)], ["2026-10-08","2026-10-09"])
        plugin=self.ctx.stars[0].star_cls
        plugin.daily_schedule_service._daily_config=lambda:{"generation_time":"-03:30"}
        self.assertEqual([str(day) for day in adapter.rolling_day_dates(at)], ["2026-10-08","2026-10-09"])

    async def test_regeneration_targets_tomorrow_waits_for_new_snapshot(self):
        from datetime import date
        plugin=self.ctx.stars[0].star_cls
        svc=plugin.daily_schedule_service
        svc.get_snapshot_for_session=lambda session,*,now:{"snapshot_id":"old","status":"ready"}
        svc.get_failure_for_session=lambda session,*,now:None
        queued=[]
        def queue(session,*,force,target_date):
            queued.append((session,force,target_date))
            async def publish():
                await __import__("asyncio").sleep(0.01)
                svc.get_snapshot_for_session=lambda session,*,now:{"snapshot_id":"new","status":"ready","slots":[{}]}
            __import__("asyncio").create_task(publish())
            return True
        svc.queue_generation=queue
        result=await TimeAwarenessAdapter(self.ctx).regenerate_date("umo",date(2026,10,8),timeout=1)
        self.assertEqual(queued,[ ("umo",True,date(2026,10,8)) ])
        self.assertEqual((result["status"],result["old_id"],result["new_id"]),("regenerated","old","new"))

    async def test_regeneration_timeout_and_queue_failure_are_honest(self):
        from datetime import date
        plugin=self.ctx.stars[0].star_cls
        svc=plugin.daily_schedule_service
        svc.register_session_async=lambda session,*,trigger=False: __import__("asyncio").sleep(0,result="ph")
        svc.get_snapshot_for_session=lambda session,*,now:{"snapshot_id":"old","status":"ready"}
        svc.get_failure_for_session=lambda session,*,now:None
        svc.queue_generation=lambda *args,**kwargs:True
        adapter=TimeAwarenessAdapter(self.ctx)
        timeout=await adapter.regenerate_date("umo",date(2026,10,8),timeout=0.01)
        self.assertEqual(timeout["status"],"timeout")
        svc.queue_generation=lambda *args,**kwargs:False
        rejected=await adapter.regenerate_date("umo",date(2026,10,8),timeout=1)
        self.assertEqual(rejected["status"],"timeout")
        self.assertIn("not queued",rejected["reason"])

    async def test_regeneration_waits_when_queue_reports_already_running(self):
        from datetime import date
        plugin=self.ctx.stars[0].star_cls
        svc=plugin.daily_schedule_service
        svc.register_session_async=lambda session,*,trigger=False: __import__("asyncio").sleep(0,result="ph")
        snapshots=[{"snapshot_id":"old","status":"ready"}]
        svc.get_snapshot_for_session=lambda session,*,now:snapshots[0]
        svc.get_failure_for_session=lambda session,*,now:None
        def queue(*args,**kwargs):
            async def publish():
                await __import__("asyncio").sleep(0.02)
                snapshots[0]={"snapshot_id":"new","status":"ready","slots":[{}]}
            __import__("asyncio").create_task(publish())
            return False  # TimeAwareness uses False for an existing persona/date task.
        svc.queue_generation=queue
        result=await TimeAwarenessAdapter(self.ctx).regenerate_date("umo",date(2026,10,8),timeout=1)
        self.assertEqual((result["status"],result["new_id"]),("regenerated","new"))

    async def test_regeneration_reports_new_terminal_failure(self):
        from datetime import date
        plugin=self.ctx.stars[0].star_cls
        svc=plugin.daily_schedule_service
        svc.register_session_async=lambda session,*,trigger=False: __import__("asyncio").sleep(0,result="ph")
        svc.get_snapshot_for_session=lambda session,*,now:{"snapshot_id":"old","status":"ready"}
        failures=[None]
        svc.get_failure_for_session=lambda session,*,now:failures[0]
        def queue(*args,**kwargs):
            async def fail():
                await __import__("asyncio").sleep(0.01)
                failures[0]={"failed_at":"later","error_type":"llm_error"}
            __import__("asyncio").create_task(fail())
            return True
        svc.queue_generation=queue
        result=await TimeAwarenessAdapter(self.ctx).regenerate_date("umo",date(2026,10,8),timeout=1)
        self.assertEqual((result["status"],result["reason"]),("failed","llm_error"))

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
        self.slots_by_date={}
        self.calls=[]; self.reg_calls=[]; self.sent=[]; self.denied=set()
        self.available_dates={self.now.date()}; self.day_kinds={self.now.date():"holiday"}
        class ScheduleService:
            async def register_session_async(_s, session, *, trigger=True): self.reg_calls.append((session,trigger)); return "persona"
            def get_snapshot_for_session(_s, session, *, now):
                return {"persona_hash":"persona","snapshot_id":"sid-"+now.date().isoformat(),"local_date":now.date().isoformat(),"timezone":"Asia/Shanghai","generated_at":"g","manually_edited":False} if now.date() in self.available_dates else None
            def _daily_config(_s): return {"generation_time":"-04:00"}
            @staticmethod
            def _parse_generation_time(value):
                raw=str(value or "00:05").strip()
                offset=1 if raw.startswith("-") else 0
                raw=raw[1:] if offset else raw
                hour,minute=(int(part) for part in raw.split(":"))
                return hour,minute,offset
        class Admin:
            def get_detail(_s, persona_hash, local_date, timezone, **kwargs):
                return {"slots":self.slots_by_date.get(local_date,self.slots)}
        def collect(*,scope,now): return types.SimpleNamespace(workday=types.SimpleNamespace(kind=self.day_kinds.get(now.date(),"unknown"),available=now.date() in self.day_kinds,value=""),now=now)
        ta=types.SimpleNamespace(daily_schedule_service=ScheduleService(),daily_schedule_admin=Admin(),time_context=types.SimpleNamespace(now=lambda:self.now,facts=types.SimpleNamespace(collect=collect)))
        class NativeFish:
            def _periods(_s): return [types.SimpleNamespace(start=9*3600,end=12*3600),types.SimpleNamespace(start=14*3600,end=18*3600)]
            def get_wallet_policy(_s, *, at=None, provider_id=None):
                at=at or self.now
                return {"enabled":True,"allowed":at.strftime("%H:%M") not in self.denied,"state":"peak" if at.strftime("%H:%M") in self.denied else "offpeak","evaluated_at":at,"timezone":"UTC","manual_override":"auto","provider_affected":True,"day_kind":"holiday","day_label":"holiday"}
        self.fish=NativeFish()
        self.conversation_rows=[types.SimpleNamespace(user_id="qq:GroupMessage:g1"),types.SimpleNamespace(user_id="qq:FriendMessage:u1")]
        async def conversations(): return self.conversation_rows
        async def persona(): return {"prompt":"persona unchanged"}
        self.provider_lookups=[]
        async def provider(umo): self.provider_lookups.append(umo); return "provider"
        async def llm(**kwargs):
            import re
            self.calls.append(kwargs)
            ids=re.findall(r'"id":\s*"([^"]+)"',kwargs["prompt"])
            return types.SimpleNamespace(completion_text=json.dumps({key:f"message-{key}" for key in ids}))
        async def send(umo,chain): self.sent.append((umo,chain))
        self.platforms={}
        self.ctx=types.SimpleNamespace(get_all_stars=lambda:[meta(TA,ta),meta(FF,self.fish)],
            conversation_manager=types.SimpleNamespace(get_conversations=conversations),
            persona_manager=types.SimpleNamespace(get_default_persona_v3=persona),
            get_current_chat_provider_id=provider,llm_generate=llm,send_message=send,
            get_platform_inst=lambda platform_id:self.platforms.get(platform_id))
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
        self.state_dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.service=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True,"provider_id":"provider"}},self.state_dir.name,fat_fish=self.policy)
        self.saved=[]
        self.service._save=lambda:self.saved.append(json.loads(json.dumps(self.service.state)))

    async def test_admin_regeneration_uses_broadcast_schedule_source_not_admin_umo(self):
        configured="configured-bot:GroupMessage:persona-source"
        fallback="qq:GroupMessage:eligible-target"
        self.service.cfg["schedule_source_umo"]=configured
        async def targets(): return [fallback]
        self.service.targets=targets
        self.assertEqual(await self.service.regeneration_source_session("admin:GroupMessage:other-chat"),configured)
        self.assertEqual(self.service.schedule_source_session([fallback]),configured)
        source_reads=[]
        class Reader:
            async def get_daily_schedule(_self,session,*,at,allow_generate):
                source_reads.append((session,allow_generate)); return {"local_date":"2026-10-08"}
        self.service.day_adapter=Reader()
        await self.service.read_schedule([fallback])
        self.assertEqual(source_reads,[(configured,False)])
        regenerated=[]
        async def regenerate(session,target_date):
            regenerated.append((session,target_date)); return {"status":"regenerated"}
        main=object.__new__(MAIN_MODULE.Main)
        main._time_awareness=types.SimpleNamespace(regenerate_date=regenerate)
        await MAIN_MODULE.Main._regenerate_schedule_dates(
            main,self.service,"admin:GroupMessage:other-chat",[self.now.date()])
        self.assertEqual(regenerated,[(configured,self.now.date())])
        self.service.cfg.pop("schedule_source_umo")
        self.assertEqual(await self.service.regeneration_source_session("admin:GroupMessage:other-chat"),fallback)

    async def test_raw_cycle_stitches_two_natural_days_at_configured_boundary(self):
        from datetime import timezone, timedelta
        self.now=datetime(2026,10,7,6,0,tzinfo=timezone(timedelta(hours=8)))
        next_day=self.now.date()+timedelta(days=1)
        self.available_dates={self.now.date(),next_day}
        self.slots_by_date={
            self.now.date():[
                {"start":"03:00","end":"05:00","name":"清晨","state":"跨过生活日边界"},
                {"start":"23:00","end":"24:00","name":"夜生活","state":"还在外面玩"},
            ],
            next_day:[
                {"start":"00:00","end":"02:00","name":"续摊","state":"继续宵夜聊天"},
                {"start":"03:00","end":"05:00","name":"睡觉","state":"终于睡了"},
            ],
        }
        cycle=await self.service.raw_cycle(now=self.now)
        self.assertEqual(cycle["boundary_clock"],"04:00")
        self.assertEqual(cycle["window_start"],"2026-10-07T04:00+08:00")
        self.assertEqual(cycle["window_end"],"2026-10-08T04:00+08:00")
        self.assertTrue(cycle["complete"])
        self.assertEqual([item["name"] for item in cycle["slots"]],
                         ["清晨","夜生活","续摊","睡觉"])
        self.assertTrue(cycle["slots"][0]["start_at"].startswith("2026-10-07T04:00"))
        self.assertTrue(cycle["slots"][-1]["end_at"].startswith("2026-10-08T04:00"))

    async def _prepare_simulation(self, slots):
        from datetime import timezone, timedelta
        self.now=datetime(2026,10,7,4,0,tzinfo=timezone(timedelta(hours=8)))
        self.available_dates={self.now.date()}
        self.day_kinds={self.now.date():"holiday"}
        self.slots=slots
        self.service.cfg["dry_run"]=False
        self.assertTrue(await self.service.refresh(now=self.now),self.service.last_error)
        return copy.deepcopy(self.service.plan_for_date(self.now.date()))

    def test_prompt_carries_concrete_normal_activity_and_natural_voice_constraints(self):
        entries=[
            {"id":"class","kind":"NORMAL","trigger_at":"08:55","slot_start":"08:55","slot_end":"12:00","name":"上午课程","state":"上课，虽然有点想翘课但还是去露个脸"},
            {"id":"exhibition","kind":"NORMAL","trigger_at":"14:00","slot_start":"14:00","slot_end":"16:00","name":"外出看展","state":"逛展厅"},
            {"id":"movie","kind":"NORMAL","trigger_at":"16:30","slot_start":"16:30","slot_end":"18:30","name":"看电影","state":"放松一下"},
            {"id":"chores","kind":"NORMAL","trigger_at":"19:00","slot_start":"19:00","slot_end":"19:30","name":"打扫家务","state":"收拾房间"},
            {"id":"meal","kind":"NORMAL","trigger_at":"19:30","slot_start":"19:30","slot_end":"20:00","name":"吃饭","state":"晚饭"},
            {"id":"walk","kind":"NORMAL","trigger_at":"20:00","slot_start":"20:00","slot_end":"20:30","name":"散步","state":"出去走走"},
        ]
        prompt=self.service._prompt_lines(entries,{"local_date":"2026-10-07"},"workday")
        for value in ("上午课程","想翘课","外出看展","看电影","打扫家务","吃饭","散步","time_segment"):
            self.assertIn(value,prompt)
        self.assertIn("独立可懂",prompt)
        self.assertIn("保留默认 Persona",prompt)
        self.assertIn("不得补造地点、人物、原因、结果",prompt)

    async def test_batch_generated_messages_remain_specific_for_everyday_activities(self):
        messages={
            "2026-10-07-N01":"上午还是去学校上课啦，虽然有点想翘课，但先去露个脸再说。",
            "2026-10-07-N02":"刚从展厅出来，今天看展走得我腿都酸啦。",
            "2026-10-07-N03":"电影散场啦，这场看得我心情好多了。",
            "2026-10-07-N04":"房间总算收拾干净了，打扫家务比想象中累欸。",
            "2026-10-07-N05":"先去吃晚饭啦，饿得我已经开始惦记下一口了。",
            "2026-10-07-N06":"饭后出来散散步，吹会儿风再回去。",
        }
        async def natural_batch(**kwargs):
            prompt=kwargs["prompt"]
            for fact in ("上午课程","外出看展","看电影","打扫家务","吃饭","散步","想翘课"):
                self.assertIn(fact,prompt)
            return types.SimpleNamespace(completion_text=json.dumps(messages,ensure_ascii=False))
        self.ctx.llm_generate=natural_batch
        await self._prepare_simulation([
            {"start":"08:00","end":"08:50","name":"上午课程","state":"上课，心里有点想翘课但决定去露个脸"},
            {"start":"09:00","end":"09:50","name":"外出看展","state":"看展"},
            {"start":"10:00","end":"10:50","name":"看电影","state":"休息"},
            {"start":"11:00","end":"11:30","name":"打扫家务","state":"收拾房间"},
            {"start":"12:00","end":"12:30","name":"吃饭","state":"午饭"},
            {"start":"13:00","end":"13:30","name":"散步","state":"出去走走"},
        ])
        plan=self.service.plan_for_date(self.now.date())
        self.assertTrue(plan["plan_complete"])
        self.assertEqual({entry["id"]:entry["message"] for entry in plan["entries"]},messages)

    def test_peak_primary_activity_comes_from_overlapping_slot_and_unknown_is_not_guessed(self):
        date=self.now.date()
        peak_start=self.now.replace(hour=9,minute=0)
        peak_end=self.now.replace(hour=12,minute=0)
        activity=self.service._primary_peak_activity({"slots":[
            {"start":"09:10","end":"11:30","name":"上午课程","state":"上课，差点想翘课"},
            {"start":"11:35","end":"11:55","name":"买饮料","state":"课间休息"},
        ]},date,"Asia/Shanghai",peak_start,peak_end)
        self.assertEqual(activity["name"],"上午课程")
        self.assertGreaterEqual(activity["duration_minutes"],90)
        unknown=self.service._primary_peak_activity({"slots":[{"start":"08:00","end":"08:30","name":"早餐","state":"吃饭"}]},date,"Asia/Shanghai",peak_start,peak_end)
        self.assertIsNone(unknown)
        short=self.service._primary_peak_activity({"slots":[{"start":"09:10","end":"09:30","name":"买咖啡","state":"课间"}]},date,"Asia/Shanghai",peak_start,peak_end)
        self.assertIsNone(short)
        pair=[
            {"id":"start","kind":"PEAK_START","activity_id":"P1","trigger_at":"08:55","activity_context":{"activity_id":"P1","primary_activity":activity}},
            {"id":"end","kind":"PEAK_END","activity_id":"P1","trigger_at":"12:05","activity_context":{"activity_id":"P1","primary_activity":activity}},
        ]
        prompt=self.service._prompt_lines(pair,{"local_date":"2026-10-07"},"workday")
        self.assertEqual(prompt.count('"name": "上午课程"'),2)
        self.assertEqual(prompt.count('"activity_id": "P1"'),2)
        unknown_pair=[dict(entry,activity_context={"activity_id":"P2","primary_activity":None}) for entry in pair]
        unknown_prompt=self.service._prompt_lines(unknown_pair,{"local_date":"2026-10-07"},"workday")
        self.assertIn('"primary_activity": null',unknown_prompt)
        self.assertIn("不得猜测或虚构",unknown_prompt)
        self.assertIn("开始说将去/开始做什么",prompt)
        self.assertIn("结束说这件事做完了",prompt)
        self.assertIn("duration_minutes 必须与 peak_duration_minutes 相称",prompt)

    def test_peak_outline_keeps_a_multi_stop_outing_without_inventing_a_long_event(self):
        date = self.now.date()
        peak_start = self.now.replace(hour=14, minute=0)
        peak_end = self.now.replace(hour=18, minute=0)
        schedule = {"slots": [
            {"start":"13:00","end":"15:30","name":"看展","state":"展厅里逛逛拍拍"},
            {"start":"15:30","end":"16:40","name":"咖啡歇脚","state":"喝杯冰饮，翻照片"},
            {"start":"16:40","end":"18:20","name":"文创小店","state":"随缘淘两样东西"},
            {"start":"20:00","end":"21:00","name":"吃晚饭","state":"去探店"},
        ]}
        self.assertIsNone(self.service._primary_peak_activity(
            schedule, date, "Asia/Shanghai", peak_start, peak_end
        ))
        outline = self.service._peak_activity_outline(
            schedule, date, "Asia/Shanghai", peak_start, peak_end
        )
        self.assertEqual([activity["name"] for activity in outline],
                         ["看展", "咖啡歇脚", "文创小店"])
        self.assertEqual([activity["overlap_minutes"] for activity in outline],
                         [90, 70, 80])
        context = {"activity_id": "P2", "primary_activity": None,
                   "peak_duration_minutes": 240, "activity_outline": outline}
        entries = [
            {"id":"peak-start", "kind":"PEAK_START", "trigger_at":"13:57",
             "activity_context":context},
            {"id":"peak-end", "kind":"PEAK_END", "trigger_at":"18:02",
             "activity_context":context},
        ]
        prompt = self.service._prompt_lines(entries, {"local_date":str(date)}, "workday")
        self.assertEqual(prompt.count('"name": "看展"'), 2)
        self.assertEqual(prompt.count('"name": "文创小店"'), 2)
        self.assertNotIn('"name": "吃晚饭"', prompt)
        self.assertIn("按 outline 的真实时间顺序", prompt)
        self.assertIn("不能假装它们是一项持续数小时的活动", prompt)
        self.assertIn("1–3件最能解释这段时间为何不在线的实质活动", prompt)
        self.assertIn("不要把刷手机、发呆、普通吃饭这类短暂过渡", prompt)

    def test_peak_temporal_context_exact_future_slots_are_not_current_or_completed(self):
        from datetime import timezone, timedelta
        date=datetime(2026,10,9).date()
        zone=timezone(timedelta(hours=8))
        raw={"local_date":str(date),"snapshot_id":"temporal-regression","slots":[
            {"start":"01:30","end":"09:20","name":"睡觉","state":"入睡"},
            {"start":"09:20","end":"10:10","name":"赖床刷手机","state":"在床上刷手机"},
            {"start":"10:10","end":"11:00","name":"出门准备","state":"洗漱换衣准备出门"},
            {"start":"11:00","end":"13:00","name":"上午闲逛","state":"咖啡店坐坐，浏览贴纸和耳饰"},
            {"start":"14:00","end":"15:00","name":"散步","state":"出去走走"},
        ]}
        start=datetime(2026,10,9,9,0,tzinfo=zone)
        end=datetime(2026,10,9,12,0,tzinfo=zone)
        self.service._peak_windows=lambda *args:[{
            "index":1,"peak_start":start,"peak_end":end,
            "cover_start":datetime(2026,10,9,8,58,tzinfo=zone),
            "cover_end":datetime(2026,10,9,12,5,tzinfo=zone),
        }]
        entries=self.service._effective_entries(
            raw,date,"Asia/Shanghai","workday","provider",
            datetime(2026,10,9,8,0,tzinfo=zone))
        peak_start=next(entry for entry in entries if entry["kind"]=="PEAK_START")
        peak_end=next(entry for entry in entries if entry["kind"]=="PEAK_END")
        self.assertEqual(peak_start["trigger_at"],"2026-10-09T08:58:00+08:00")
        context=peak_start["activity_context"]
        self.assertEqual([item["name"] for item in context["active_at_trigger"]],["睡觉"])
        self.assertEqual([item["name"] for item in context["completed_before_trigger"]],[])
        self.assertEqual([item["name"] for item in context["upcoming_after_trigger"]],
                         ["赖床刷手机","出门准备","上午闲逛"])
        self.assertEqual(context["trigger_at"],peak_start["trigger_at"])
        self.assertEqual([item["name"] for item in peak_end["activity_context"]["active_at_trigger"]],
                         ["上午闲逛"])
        self.assertNotIn("上午闲逛",[item["name"] for item in peak_end["activity_context"]["completed_before_trigger"]])
        self.assertEqual([item["name"] for item in peak_end["activity_context"]["upcoming_after_trigger"]],
                         ["散步"])
        prompt=self.service._prompt_lines([peak_start,peak_end],raw,"workday")
        self.assertIn('"active_at_trigger": [{"name": "睡觉"',prompt)
        self.assertIn('"upcoming_after_trigger": [{"name": "赖床刷手机"',prompt)
        self.assertIn("不得把 upcoming_after_trigger 中的事写成已经发生、正在发生或已经到达目的地",prompt)
        self.assertIn("PEAK_END 只能把 completed_before_trigger 中的事情说成已完成",prompt)
        self.assertIn("日程写翻看/浏览贴纸或耳饰，绝不表示买了",prompt)

    def test_peak_outline_skips_nonoverlapping_and_caps_context(self):
        date = self.now.date()
        peak_start = self.now.replace(hour=9, minute=0)
        peak_end = self.now.replace(hour=12, minute=0)
        slots = [
            {"start":"08:30","end":"09:00","name":"早饭","state":"吃东西"},
            *({"start":f"09:{n:02d}","end":f"09:{n+5:02d}",
               "name":f"活动{n}","state":"短时间活动"} for n in range(0,40,5)),
            {"start":"12:00","end":"12:30","name":"午饭","state":"吃东西"},
        ]
        outline = self.service._peak_activity_outline(
            {"slots":slots}, date, "Asia/Shanghai", peak_start, peak_end
        )
        self.assertEqual(len(outline), 6)
        self.assertEqual([item["name"] for item in outline],
                         ["活动0", "活动5", "活动10", "活动15", "活动20", "活动25"])
        self.assertTrue(all(item["overlap_minutes"] == 5 for item in outline))

    async def test_nightlife_slots_are_broadcast_as_fact_not_added_by_xiaoman(self):
        plan_before = await self._prepare_simulation([
            {"start":"21:30","end":"22:30","name":"去看演出","state":"和朋友听音乐"},
            {"start":"23:15","end":"24:00","name":"深夜宵夜","state":"边吃边聊"},
        ])
        entries = self.service.plan_for_date(self.now.date())["entries"]
        self.assertEqual([entry["name"] for entry in entries],
                         ["去看演出", "深夜宵夜"])
        self.assertEqual([entry["kind"] for entry in entries], ["NORMAL", "NORMAL"])
        self.assertEqual(self.service.plan_for_date(self.now.date()), plan_before)
        prompt = self.service._prompt_lines(entries, {"local_date":str(self.now.date())},
                                            "holiday")
        self.assertIn("不要默认23点就必须睡觉", prompt)
        self.assertIn("绝不能为制造夜生活而补造日程中没有的活动", prompt)

    async def test_prompt_rebuild_preserves_successfully_delivered_old_event(self):
        old={"id":"old-event","trigger_at":self.now.isoformat(),"message":"已发送的旧文案","sent":True,
             "expired":False,"delivered_umos":["qq:GroupMessage:g1"]}
        rebuilt={"id":"old-event","trigger_at":self.now.isoformat(),"message":"","sent":False,
                 "expired":False,"delivered_umos":[]}
        preserved=self.service._preserve_delivery({"entries":[old]},[rebuilt])[0]
        self.assertEqual(preserved["message"],"已发送的旧文案")
        self.assertTrue(preserved["sent"])
        self.assertEqual(preserved["delivered_umos"],["qq:GroupMessage:g1"])
        self.service.state["plans"][self.now.date().isoformat()]={"timezone":"Asia/Shanghai","entries":[preserved]}
        result=await self.service.send_due(self.now)
        self.assertEqual(result["success_count"],0)
        self.assertEqual(self.sent,[])

    def _aiocqhttp(self, *instance_ids):
        for instance_id in instance_ids:
            self.platforms[instance_id]=types.SimpleNamespace(meta=lambda:types.SimpleNamespace(name="aiocqhttp"))

    async def test_physical_qq_group_aliases_deduplicate_and_distinct_groups_remain(self):
        self._aiocqhttp("bot-a")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:979675497_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:123456789_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387216",platform_id="bot-a"),
        ]
        targets=await self.service.targets()
        self.assertEqual(targets,["bot-a:GroupMessage:1153387215","bot-a:GroupMessage:1153387216"])

    async def test_group_private_flags_and_alias_deny_allow(self):
        self._aiocqhttp("bot-a")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:979675497_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:FriendMessage:1153387215",platform_id="bot-a"),
        ]
        self.service.cfg.update(send_groups=False,send_private=True)
        self.assertEqual(await self.service.targets(),["bot-a:FriendMessage:1153387215"])
        self.service.cfg.update(send_groups=True,send_private=False,
                                allowlist_umos=["bot-a:GroupMessage:979675497_1153387215"],
                                denylist_umos=["bot-a:GroupMessage:1153387215"])
        self.assertEqual(await self.service.targets(),[])
        self.service.cfg["denylist_umos"]=[]
        self.assertEqual(await self.service.targets(),["bot-a:GroupMessage:1153387215"])

    async def test_other_platforms_and_bot_instances_are_not_merged(self):
        self._aiocqhttp("bot-a","bot-b")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:979675497_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-b:GroupMessage:1153387215",platform_id="bot-b"),
            types.SimpleNamespace(user_id="other-bot:GroupMessage:1153387215",platform_id="other-bot"),
        ]
        self.assertEqual(len(await self.service.targets()),3)

    async def test_invalid_umo_is_not_canonicalized(self):
        self._aiocqhttp("bot-a")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:bad_1153387215_extra",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:",platform_id="bot-a"),
        ]
        self.assertEqual(await self.service.targets(),["bot-a:GroupMessage:1153387215","bot-a:GroupMessage:bad_1153387215_extra"])

    async def test_simulated_event_sends_once_to_one_physical_qq_group(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        self._aiocqhttp("bot-a")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:979675497_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:123456789_1153387215",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387216",platform_id="bot-a"),
            types.SimpleNamespace(user_id="bot-a:GroupMessage:987654321_1153387216",platform_id="bot-a"),
        ]
        original=await self._prepare_simulation([{"start":"10:00","end":"10:05","name":"event","state":"x"}])
        result,error=await self.service.simulate_time("10:00")
        self.assertFalse(error)
        self.assertEqual((result["target_count"],result["success_count"],result["failure_count"]),(2,2,0))
        self.assertEqual([umo for umo,_ in self.sent],["bot-a:GroupMessage:1153387215","bot-a:GroupMessage:1153387216"])
        self.assertEqual(self.service.plan_for_date(self.now.date()),original)

    async def test_legacy_delivered_alias_suppresses_physical_group_resend(self):
        self._aiocqhttp("bot-a")
        self.conversation_rows=[
            types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215",platform_id="bot-a"),
        ]
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        entry=self.service.plan_for_date(self.now.date())["entries"][0]
        entry["delivered_umos"]=["bot-a:GroupMessage:979675497_1153387215"]
        result=await self.service.send_due(self.now.replace(hour=20,minute=30))
        self.assertEqual(result["success_count"],0)
        self.assertTrue(entry["sent"])
        self.assertEqual(self.sent,[])

    async def test_send_message_false_is_reported_and_retried(self):
        import astrbot.api.event
        class Chain:
            def message(self,value): return self
        astrbot.api.event.MessageChain=Chain
        self.slots=[{"start":"20:30","name":"x","state":"y"}]
        await self.service.refresh(now=self.now)
        calls=[]
        async def false_send(umo,chain): calls.append(umo); return False
        self.ctx.send_message=false_send
        now=self.now.replace(hour=20,minute=30)
        result=await self.service.send_due(now)
        entry=self.service.plan_for_date(self.now.date())["entries"][0]
        self.assertFalse(entry["sent"])
        self.assertEqual(result["failure_count"],2)
        self.assertTrue(all(item["reason"]=="send_message returned False" for item in result["failures"]))
        self.assertEqual(entry["delivered_umos"],[])

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
        reloaded=ScheduleBroadcastService(self.ctx,{"schedule_broadcast":{"enable":True}},self.state_dir.name)
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
        for start_entry in starts:
            end_entry=next(entry for entry in ends if entry["activity_id"]==start_entry["activity_id"])
            self.assertIsNone(start_entry["activity_context"]["primary_activity"])
            self.assertNotEqual(start_entry["activity_context"]["trigger_at"],end_entry["activity_context"]["trigger_at"])
            # These sample sub-slots (15–20 minutes) are too short to represent
            # the 3–4 hour Fat Fish peak windows as one coherent start/end activity.
            self.assertGreaterEqual(start_entry["activity_context"]["peak_duration_minutes"],180)
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
