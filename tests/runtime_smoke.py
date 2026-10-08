"""AstrBot 4.28.2 tool smoke and official Airi prefix-mode compatibility."""

from __future__ import annotations

import asyncio
import ast
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import re
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

from astrbot.api.star import Context as AstrBotContext, Star
from astrbot.api.event import MessageChain
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.star.star import StarMetadata
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import StarHandlerRegistry


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"
ANGELHEART_COMMIT = "a1e1b0583e6578d2048f14e57122ced6db498058"


def install_package_path(name: str, path: Path) -> None:
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    package.__package__ = name
    sys.modules[name] = package


def load_main():
    for module_name in list(sys.modules):
        if module_name == PACKAGE_NAME or module_name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[module_name]
    install_package_path("data", ROOT.parent.parent.parent)
    install_package_path("data.plugins", ROOT.parent.parent)
    install_package_path(PACKAGE_NAME, ROOT)
    return importlib.import_module(f"{PACKAGE_NAME}.main")


def load_official_airi_helpers(airi_root: Path):
    metadata = (airi_root / "metadata.yaml").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*v2\.11\.15\s*$", metadata, re.MULTILINE)

    def load_helper(module_name: str, filename: str):
        spec = importlib.util.spec_from_file_location(module_name, airi_root / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    commands = load_helper("official_airi_gallery_commands", "gallery_commands.py")
    config = load_helper("official_airi_gallery_config", "gallery_config.py")

    # Confirm the official plugin wires configured prefix mode into the parser.
    main_source = (airi_root / "main.py").read_text(encoding="utf-8")
    assert "use_prefix=self.view_command_mode == MODE_PREFIX" in main_source
    assert 'resolve_view_command_mode(self.config)' in main_source
    return commands, config


def load_angelheart_sources(angelheart_root: Path | None):
    """Read the pinned official source locally or directly from its commit."""
    if angelheart_root is not None:
        if (angelheart_root / ".git").exists():
            revision = subprocess.check_output(
                ["git", "-C", str(angelheart_root), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            assert revision == ANGELHEART_COMMIT
        metadata = (angelheart_root / "metadata.yaml").read_text(encoding="utf-8")
        source = (angelheart_root / "main.py").read_text(encoding="utf-8")
    else:
        base = f"https://raw.githubusercontent.com/kawayiYokami/astrbot_plugin_angel_heart/{ANGELHEART_COMMIT}"
        try:
            with urllib.request.urlopen(f"{base}/metadata.yaml", timeout=20) as response:
                metadata = response.read().decode("utf-8")
            with urllib.request.urlopen(f"{base}/main.py", timeout=20) as response:
                source = response.read().decode("utf-8")
        except urllib.error.URLError:
            print("AngelHeart pinned-source audit skipped: GitHub network unavailable")
            return None

    assert re.search(r"^version:\s*2\.2\.8\s*$", metadata, re.MULTILINE)
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "smart_reply_handler" in methods

    def call_name(node):
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return ""

    def literal(node):
        if isinstance(node, ast.Constant):
            return node.value
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
        ):
            return -node.operand.value
        return None

    ingress = methods["smart_reply_handler"]
    ingress_priorities = [
        literal(keyword.value)
        for decorator in ingress.decorator_list
        if isinstance(decorator, ast.Call) and call_name(decorator) == "event_message_type"
        for keyword in decorator.keywords
        if keyword.arg == "priority"
    ]
    assert -10 in ingress_priorities

    expected_hooks = {
        "on_llm_request",
        "register_on_agent_done",
        "on_decorating_result",
        "after_message_sent",
    }
    registered_hooks = {
        call_name(decorator)
        for node in methods.values()
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
    }
    assert expected_hooks <= registered_hooks
    return source


def verify_astrbot_plugin_filter_contract():
    dispatch_source = inspect.getsource(call_event_hook)
    registry_source = inspect.getsource(StarHandlerRegistry.get_handlers_by_event_type)
    assert "plugins_name=event.plugins_name" in dispatch_source
    assert "plugins_name" in registry_source
    assert "plugin.name not in plugins_name" in registry_source


async def verify_schedule_bridge_public_contract():
    """Exercise the adapter against AstrBot 4.28.2 public API types/signatures."""
    assert hasattr(AstrBotContext, "get_all_stars")
    assert "chat_provider_id" in inspect.signature(AstrBotContext.llm_generate).parameters
    assert "message_chain" in inspect.signature(AstrBotContext.send_message).parameters
    assert "umo" in inspect.signature(AstrBotContext.get_current_chat_provider_id).parameters
    assert inspect.iscoroutinefunction(ConversationManager.get_conversations)
    assert "star_cls" in StarMetadata.__annotations__
    assert "activated" in StarMetadata.__annotations__
    assert hasattr(PersonaManager, "get_default_persona_v3")
    assert isinstance(MessageChain().message("probe"), MessageChain)

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.schedule_broadcast import ScheduleBroadcastService

    clock = datetime(2026, 10, 9, 4, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    class RuntimeTimeAwareness:
        config = {"daily_schedule": {"ai_daily": {"generation_time": "-04:00", "worldview": "runtime worldview",
                  "use_persona": True, "adaptive": {"theme_pool": ["creative"], "style_pool": ["natural"],
                  "allow_custom_theme": True, "recent_days": 3, "state_continuity_enabled": True}}}}
        time_context = types.SimpleNamespace(
            now=lambda: clock,
            facts=types.SimpleNamespace(collect=lambda **kwargs: types.SimpleNamespace(
                workday=types.SimpleNamespace(kind="workday", available=True, value="workday"),
                now=kwargs["now"])),
        )

    class RuntimeDayAdapter:
        def __init__(self): self.plugin = RuntimeTimeAwareness()
        def current_time(self): return clock
        def discover(self): return self.plugin
        def get_generation_boundary(self):
            return {"available": True, "raw": "-04:00", "clock": "04:00", "hour": 4,
                    "minute": 0, "target_day_offset": 1}
        def life_day_window(self, at=None):
            start = (at or clock).replace(hour=4, minute=0, second=0, microsecond=0)
            if (at or clock) < start: start -= timedelta(days=1)
            return {**self.get_generation_boundary(), "start": start, "end": start+timedelta(days=1),
                    "timezone": "Asia/Shanghai"}
        async def planner_context(self, start, end):
            return {"available": True, "calendar_days": [{"date": start.date().isoformat(),
                    "kind": "workday", "label": "workday"}], "worldview": "runtime worldview",
                    "use_persona": True, "theme_pool": ["creative"], "style_pool": ["natural"],
                    "allow_custom_theme": True, "adaptive": {"recent_days": 3,
                    "state_continuity_enabled": True}, "weather": []}

    class RuntimeFish:
        # Same public shape as Fat Fish Wallet v1.1.1: configuration only.
        config = {"enabled": False, "timezone": "Asia/Shanghai", "manual_override": "auto",
                  "peak_periods": "09:00-12:00,14:00-18:00", "peak_weekdays": "0,1,2,3,4,5,6",
                  "affected_providers": "deepseek", "gate_when_provider_unknown": True}

    class PublicContextFixture:
        def __init__(self):
            self.get_all_stars_called = 0
            self.llm_calls = []
            self.sent = []
            self.fish = RuntimeFish()
            self.persona_manager = types.SimpleNamespace(
                get_default_persona_v3=self.get_persona
            )
            self.conversation_manager = types.SimpleNamespace(get_conversations=self.get_conversations)

        async def get_conversations(self):
            return [types.SimpleNamespace(user_id="qq:GroupMessage:runtime-broadcast", platform_id="qq")]

        async def get_current_chat_provider_id(self, umo):
            assert umo == "qq:GroupMessage:runtime-broadcast"
            return "runtime-provider"

        def get_provider_by_id(self, provider_id):
            assert provider_id == "runtime-provider"
            return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(
                id=provider_id, model="deepseek-chat", type="llm"))

        def get_all_stars(self):
            return [types.SimpleNamespace(name="astrbot_plugin_fat_fish_wallet",
                                          activated=True, star_cls=self.fish)]

        async def get_persona(self):
            return {"name": "runtime", "prompt": "runtime dict persona"}

        async def llm_generate(self, *, chat_provider_id, prompt=None, tools=None, system_prompt=None, **kwargs):
            self.llm_calls.append((chat_provider_id, prompt, tools, system_prompt))
            planner_input = json.loads(prompt.split("PLANNER_INPUT:\n", 1)[1])
            return types.SimpleNamespace(completion_text=json.dumps({
                "daily_theme": "runtime theme", "daily_style": "runtime style",
                "free_window_plans": {
                    free["id"]: [{"category": "rest", "name": "休息", "state": "在家休息",
                                  "broadcast_message": "今天先按自己的节奏休息。", "weight": 1}]
                    for free in planner_input["free_windows"]
                },
                "bridge_plans": {
                    protected["id"]: {"category": "rest", "name": "保护窗内休息",
                                      "state": "安静休息。", "enter_message": "先去休息啦。",
                                      "exit_message": "休息结束了。"}
                    for protected in planner_input["protected_windows"]
                }}, ensure_ascii=False))

        async def send_message(self, session, message_chain):
            assert isinstance(message_chain, MessageChain)
            self.sent.append((session, message_chain))

    runtime_context = PublicContextFixture()
    from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.fat_fish_bridge import FatFishBridge
    config = {"schedule_broadcast": {"enable": True}}
    service = ScheduleBroadcastService(runtime_context, config, ".",
                                       time_awareness=RuntimeDayAdapter(),
                                       fat_fish=FatFishBridge(runtime_context, RuntimeDayAdapter()))
    service._save = lambda: None
    result = await service.generate_life_day(clock)
    assert result["status"] == "generated"
    assert len(runtime_context.llm_calls) == 1
    assert runtime_context.llm_calls[0][0] == "runtime-provider"
    assert runtime_context.llm_calls[0][2] is None
    assert runtime_context.llm_calls[0][3] == "runtime dict persona"
    assert service.current_plan(clock)["timeline"][0]["name"] == "休息"
    assert service.current_plan(clock)["planner_version"] == "0.9.1"
    assert service.current_plan(clock)["schema_version"] == 2
    assert {"id", "kind", "start_at", "end_at"}.issubset(service.current_plan(clock)["timeline"][0])
    call_count = len(runtime_context.llm_calls)
    await service.refresh()
    await service.send_due(clock)
    assert len(runtime_context.llm_calls) == call_count
    assert len(runtime_context.sent) == 1
    assert service.current_plan(clock)["deliveries"][0]["sent"] is True

    await verify_schedule_broadcast_routes_by_real_platform_id()


async def verify_schedule_broadcast_routes_by_real_platform_id() -> None:
    """Exercise Xiaoman's send_due through AstrBot 4.28.2's real Context router."""
    from datetime import datetime
    from astrbot.core.star.context import Context as CoreContext
    from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.schedule_broadcast import ScheduleBroadcastService

    class RoutedPlatform:
        def __init__(self, platform_id: str) -> None:
            self._meta = types.SimpleNamespace(id=platform_id, name="aiocqhttp")
            self.sent = []
        def meta(self):
            return self._meta
        async def send_by_session(self, session, message_chain):
            # Mirrors aiocqhttp's actual final group target extraction after the
            # real AstrBot Context has routed to the adapter by platform ID.
            group_id = session.session_id.split("_")[-1]
            self.sent.append((session.platform_id, group_id, message_chain))

    platforms = [RoutedPlatform("bot-a"), RoutedPlatform("bot-b")]
    rows = [
        types.SimpleNamespace(user_id="bot-a:GroupMessage:111_1153387215", platform_id="bot-a"),
        types.SimpleNamespace(user_id="bot-a:GroupMessage:1153387215", platform_id="bot-a"),
        types.SimpleNamespace(user_id="bot-b:GroupMessage:222_1153387215", platform_id="bot-b"),
        types.SimpleNamespace(user_id="bot-b:GroupMessage:1153387215", platform_id="bot-b"),
    ]
    context = object.__new__(CoreContext)
    context.platform_manager = types.SimpleNamespace(platform_insts=platforms)
    context.astrbot_config_mgr = types.SimpleNamespace(get_conf=lambda umo: {})
    context._config = {}
    context.conversation_manager = types.SimpleNamespace(get_conversations=lambda: _async_result(rows))
    now = datetime.now().astimezone()
    service = ScheduleBroadcastService(context, {"schedule_broadcast": {}}, ".",
                                       time_awareness=object(), fat_fish=object())
    service._save = lambda: None
    service.state = {"schema_version": 2, "generation_attempts": {}, "plans": {
        now.isoformat(): {"timezone": "Asia/Shanghai", "deliveries": [
            {"id": "route-smoke", "trigger_at": now.isoformat(), "message": "route check",
             "sent": False, "expired": False, "delivered_umos": []}],
        }}}
    result = await service.send_due(now)
    assert result["target_count"] == result["success_count"] == 2
    assert result["failure_count"] == 0
    assert {tuple(item[:2]) for platform in platforms for item in platform.sent} == {
        ("bot-a", "1153387215"), ("bot-b", "1153387215")
    }
    assert all(len(platform.sent) == 1 for platform in platforms)


async def _async_result(value):
    return value


class Context:
    def __init__(self) -> None:
        self.tools = []
        self.tool_manager = None
        self.stars = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def get_llm_tool_manager(self):
        return self.tool_manager

    def get_all_stars(self):
        return self.stars


class GalleryTool:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls = []

    async def call(self, current_context, **kwargs):
        self.calls.append((current_context, kwargs))
        return self.result


class ToolManager:
    def __init__(self, gallery_tool):
        self.gallery_tool = gallery_tool

    def get_func(self, name):
        assert name == "gallery_send"
        return self.gallery_tool


async def run(official_airi_root: Path | None = None) -> None:
    assert importlib.metadata.version("AstrBot") == "4.28.2"
    verify_astrbot_plugin_filter_contract()
    main = load_main()
    await verify_schedule_bridge_public_contract()
    context = Context()
    plugin = main.Main(context)
    assert isinstance(plugin, Star)
    assert [tool.name for tool in context.tools] == ["send_xiaoman_photo"]
    assert type(context.tools[0]).__module__ == f"{PACKAGE_NAME}.tools.photo_tool"
    assert context.tools[0].parameters["properties"] == {}
    assert not hasattr(plugin, "guard_directed_gallery_route")

    # Exercise event-local bypass with the actual AstrBot runtime loaded.
    from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.test_bypass import (
        ANGELHEART_HANDLER_NAME,
        ANGELHEART_PLUGIN_NAME,
        TEST_GUIDANCE,
        inject_test_guidance,
    )

    class Handler:
        def __init__(self, module, name):
            self.handler_module_path = module
            self.handler_name = name

    activated = [
        Handler("data.plugins.astrbot_plugin_angel_heart.main", ANGELHEART_HANDLER_NAME),
        Handler("data.plugins.other.main", "normal_handler"),
    ]
    message = "小满小满，给我看看你的照片"
    bypass_event = types.SimpleNamespace(
        message_str=message,
        unified_msg_origin="qq:group:runtime-smoke",
        plugins_name=None,
        is_at_or_wake_command=False,
        get_sender_id=lambda: "979675497",
        get_extra=lambda name, default=None: activated if name == "activated_handlers" else default,
        stop_event=lambda: (_ for _ in ()).throw(AssertionError("stop_event called")),
    )
    context.stars = [
        types.SimpleNamespace(name=ANGELHEART_PLUGIN_NAME, activated=True),
        types.SimpleNamespace(name="other", activated=True),
        types.SimpleNamespace(name="inactive", activated=False),
    ]
    plugin._test_mode_umos.add(bypass_event.unified_msg_origin)
    await plugin.bypass_angelheart_for_test_event(bypass_event)
    assert bypass_event.plugins_name == ["other"]
    assert len(activated) == 1 and activated[0].handler_name == "normal_handler"
    assert bypass_event.is_at_or_wake_command
    assert bypass_event.message_str == message
    guidance_req = ProviderRequest(
        extra_user_content_parts=[],
        system_prompt="unchanged system",
        prompt=message,
    )
    assert inject_test_guidance(
        bypass_event, {bypass_event.unified_msg_origin}, guidance_req
    )
    assert not inject_test_guidance(
        bypass_event, {bypass_event.unified_msg_origin}, guidance_req
    )
    assert len(guidance_req.extra_user_content_parts) == 1
    guidance_part = guidance_req.extra_user_content_parts[0]
    assert isinstance(guidance_part, TextPart)
    assert guidance_part.text == TEST_GUIDANCE
    assert guidance_part.model_dump_for_context().get("_no_save") is True
    assert guidance_req.system_prompt == "unchanged system"
    assert guidance_req.prompt == message
    assembled = await guidance_req.assemble_context()
    assembled_content = assembled["content"]
    assert isinstance(assembled_content, list)
    guidance_blocks = [
        block
        for block in assembled_content
        if block.get("type") == "text" and block.get("text") == TEST_GUIDANCE
    ]
    assert len(guidance_blocks) == 1
    assert guidance_blocks[0].get("_no_save") is True
    assert bypass_event.message_str == message

    # Exercise the actual AstrBot 4.28.2 request-local ToolSet behavior.
    req = types.SimpleNamespace(
        func_tool=ToolSet(
            tools=[
                FunctionTool(name="send_xiaoman_photo", description="wrapper", parameters={"type": "object", "properties": {}}),
                FunctionTool(name="gallery_send", description="gallery", parameters={"type": "object", "properties": {}}),
                FunctionTool(name="tts_speak", description="voice", parameters={"type": "object", "properties": {}}),
            ]
        )
    )
    await plugin.hide_delegated_gallery_tool(None, req)
    assert req.func_tool.names() == ["send_xiaoman_photo", "tts_speak"]

    # The wrapper continues resolving the registered Airi tool and delegates
    # using the current FunctionTool context with fixed arguments.
    current_context = object()
    gallery = GalleryTool(" 已从  林小满 分类发送 1 张图片！ ")
    context.tool_manager = ToolManager(gallery)
    assert await context.tools[0].call(current_context) == "PHOTO_SENT"
    assert gallery.calls == [(current_context, {"category": "林小满", "count": 1})]

    context.tool_manager = ToolManager(GalleryTool("林小满分类中没有可用图片"))
    assert await context.tools[0].call(current_context) == "PHOTO_SEND_FAILED"

    # Verify behavior from the pinned official Airi Gallery command parser.
    if official_airi_root is not None:
        airi, airi_config = load_official_airi_helpers(official_airi_root)
        assert airi_config.resolve_view_command_mode({"view_command_mode": "prefix"}) == airi_config.MODE_PREFIX
        for ordinary_text in (
            "看看小满",
            "看看默认",
            "看看123",
            "看100-110",
            "看全部小满",
        ):
            assert airi.extract_view_target(ordinary_text, use_prefix=True) is None
            assert airi.extract_view_all_target(ordinary_text, use_prefix=True) is None

        assert airi.extract_view_target("/看看小满", use_prefix=True) == "小满"
        assert airi.extract_view_target("/看看默认", use_prefix=True) == "默认"
        assert airi.extract_view_target("/看看123", use_prefix=True) == "123"
        assert airi.parse_view_target(airi.extract_view_target("/看100-110", use_prefix=True)) == (
            "range",
            (100, 110),
        )
        assert airi.extract_view_all_target("/看全部小满", use_prefix=True) == "小满"

    production_sources = [
        (ROOT / "main.py").read_text(encoding="utf-8"),
        *(path.read_text(encoding="utf-8") for path in (ROOT / "services").glob("*.py")),
        *(path.read_text(encoding="utf-8") for path in (ROOT / "tools").glob("*.py")),
    ]
    source = "\n".join(production_sources)
    for removed_route_marker in (
        "guard_directed_gallery_route",
        "guard_directed_look_request",
        "gallery_route_adapter",
        "stop_event",
        'startswith("看")',
    ):
        assert removed_route_marker not in source

    if official_airi_root is not None:
        angelheart_root = official_airi_root.parent / "official-angel-heart-audit"
    else:
        angelheart_root = ROOT.parent / "official-angel-heart-audit"
    if not (angelheart_root / "main.py").exists():
        angelheart_root = None
    load_angelheart_sources(angelheart_root)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
    asyncio.run(run(root))
    print("AstrBot 4.28.2 photo-tool, life-day planner, admin-bypass, AngelHeart, and Airi smoke passed")
