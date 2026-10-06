"""Minimal AstrBot v4.28.2 runtime smoke for the clean photo-only plugin."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import inspect
import sys
import types
from pathlib import Path
import re

from astrbot.api.star import Star
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.message.components import Plain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.session_plugin_manager import SessionPluginManager


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"
AIRI_MODULE = "data.plugins.astrbot_plugin_airi_gallery.main"


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


def load_official_airi_handler(airi_root: Path):
    metadata = (airi_root / "metadata.yaml").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*v2\.11\.15\s*$", metadata, re.MULTILINE)

    package_name = "data.plugins.astrbot_plugin_airi_gallery"
    package = types.ModuleType(package_name)
    package.__path__ = [str(airi_root)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    module = importlib.import_module(f"{package_name}.main")

    from astrbot.core.star.star_handler import star_handlers_registry

    handler = next(
        item
        for item in star_handlers_registry._handlers
        if item.handler_module_path == module.__name__
        and item.handler_name == "handle_gallery_message"
    )
    assert handler.extras_configs.get("priority") == 1
    return handler


class Context:
    def __init__(self) -> None:
        self.tools = []
        self.tool_manager = None

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def get_llm_tool_manager(self):
        return self.tool_manager


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


class Handler:
    def __init__(self, module_path: str, handler_name: str) -> None:
        self.handler_module_path = module_path
        self.handler_name = handler_name


class Event:
    def __init__(self, handlers) -> None:
        self.is_at_or_wake_command = True
        self.message_str = "看看你的照片"
        self._handlers = handlers
        self.stop_calls = 0

    def get_extra(self, key: str):
        if key != "activated_handlers":
            raise KeyError(key)
        return self._handlers

    def stop_event(self) -> None:
        self.stop_calls += 1


class WakingEvent(Event):
    """Small event double for the real v4.28.2 WakingCheckStage.process."""

    def __init__(self, message: str) -> None:
        super().__init__([])
        self.message_str = message
        self.message_obj = types.SimpleNamespace(type=MessageType.GROUP_MESSAGE)
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.role = "member"
        self.plugins_name = None
        self.unified_msg_origin = "qq:group:smoke"

    def set_extra(self, key: str, value) -> None:
        if not hasattr(self, "_extras"):
            self._extras = {}
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default=None):
        extras = getattr(self, "_extras", {})
        return extras.get(key, default) if key is not None else extras

    def get_messages(self):
        return [Plain(self.message_str)]

    def get_message_str(self) -> str:
        return self.message_str

    def get_sender_id(self) -> str:
        return "sender-smoke"

    def get_self_id(self) -> str:
        return "bot-smoke"

    def get_group_id(self) -> str:
        return "group-smoke"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def is_private_chat(self) -> bool:
        return False

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    async def send(self, _result) -> None:
        raise AssertionError("WakingCheck should not send a response in this smoke")


async def run(official_airi_root: Path | None = None) -> None:
    assert importlib.metadata.version("AstrBot") == "4.28.2"
    main = load_main()
    context = Context()
    plugin = main.Main(context)
    assert isinstance(plugin, Star)
    assert [tool.name for tool in context.tools] == ["send_xiaoman_photo"]
    assert type(context.tools[0]).__module__ == f"{PACKAGE_NAME}.tools.photo_tool"
    assert "照片" in context.tools[0].description
    assert context.tools[0].parameters["properties"] == {}

    # Exercise the real AstrBot 4.28.2 ToolSet API on a request-local set.
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

    # The wrapper still resolves the global tool and delegates on the same context.
    current_context = object()
    gallery = GalleryTool(" 已从  林小满 分类发送 1 张图片！ ")
    context.tool_manager = ToolManager(gallery)
    assert await context.tools[0].call(current_context) == "PHOTO_SENT"
    assert gallery.calls == [(current_context, {"category": "林小满", "count": 1})]

    # Explicitly test a normal failure response; it is not leaked to the LLM.
    context.tool_manager = ToolManager(GalleryTool("林小满分类中没有可用图片"))
    assert await context.tools[0].call(current_context) == "PHOTO_SEND_FAILED"

    airi_handler = (
        load_official_airi_handler(official_airi_root)
        if official_airi_root is not None
        else Handler(AIRI_MODULE, "handle_gallery_message")
    )
    handlers = [
        Handler("data.plugins.other.main", "before"),
        airi_handler,
        Handler(AIRI_MODULE, "cmd_gallery_help"),
    ]
    handlers_id = id(handlers)
    event = Event(handlers)
    await plugin.guard_directed_gallery_route(event)

    assert id(handlers) == handlers_id
    assert event.message_str == "看看你的照片"
    assert event.stop_calls == 0
    assert [(handler.handler_module_path, handler.handler_name) for handler in handlers] == [
        ("data.plugins.other.main", "before"),
        (AIRI_MODULE, "cmd_gallery_help"),
    ]

    # Exercise AstrBot's real wake-prefix stripping and activated-handler selection.
    config = {
        "platform_settings": {
            "friend_message_needs_wake_prefix": False,
            "ignore_bot_self_message": False,
            "ignore_at_all": False,
            "unique_session": False,
        },
        "admins_id": [],
        "wake_prefix": ["小满小满"],
        "disable_builtin_commands": False,
        "plugin_set": ["*"],
    }
    waking = WakingCheckStage()
    await waking.initialize(
        types.SimpleNamespace(
            astrbot_config=config,
            db_helper=None,
            astrbot_config_id="runtime-smoke",
        )
    )
    original_filter = SessionPluginManager.filter_handlers_by_session

    async def keep_handlers(_event, activated):
        return activated

    SessionPluginManager.filter_handlers_by_session = staticmethod(keep_handlers)
    try:
        for incoming, expected_reroute in (
            ("小满小满，给我看看照片", True),
            ("小满小满来张自拍", True),
            ("小满小满看看天文台照片", False),
            ("小满小满看看默认", False),
        ):
            waking_event = WakingEvent(incoming)
            await waking.process(waking_event)
            assert waking_event.is_at_or_wake_command is True
            assert waking_event.message_str == incoming[len("小满小满") :].strip()
            activated = waking_event.get_extra("activated_handlers")
            activated_id = id(activated)
            assert any(
                item.handler_module_path == AIRI_MODULE
                and item.handler_name == "handle_gallery_message"
                for item in activated
            )
            await plugin.guard_directed_gallery_route(waking_event)
            assert id(waking_event.get_extra("activated_handlers")) == activated_id
            has_generic = any(
                item.handler_module_path == AIRI_MODULE
                and item.handler_name == "handle_gallery_message"
                for item in activated
            )
            assert has_generic is (not expected_reroute)
            assert waking_event.stop_calls == 0
    finally:
        SessionPluginManager.filter_handlers_by_session = original_filter

    assert "on_llm_request" in inspect.getsource(main.Main.hide_delegated_gallery_tool)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
    asyncio.run(run(root))
    print("AstrBot 4.28.2 Xiaoman route-guard runtime smoke passed")
