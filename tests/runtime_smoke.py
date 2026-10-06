"""AstrBot 4.28.2 tool smoke and official Airi prefix-mode compatibility."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import re
import sys
import types
from pathlib import Path

from astrbot.api.star import Star
from astrbot.core.agent.tool import FunctionTool, ToolSet


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"


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


async def run(official_airi_root: Path | None = None) -> None:
    assert importlib.metadata.version("AstrBot") == "4.28.2"
    main = load_main()
    context = Context()
    plugin = main.Main(context)
    assert isinstance(plugin, Star)
    assert [tool.name for tool in context.tools] == ["send_xiaoman_photo"]
    assert type(context.tools[0]).__module__ == f"{PACKAGE_NAME}.tools.photo_tool"
    assert context.tools[0].parameters["properties"] == {}
    assert not hasattr(plugin, "guard_directed_gallery_route")

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
        "event_message_type",
        "guard_directed_gallery_route",
        "gallery_route_adapter",
        "message_str",
        "stop_event",
    ):
        assert removed_route_marker not in source


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
    asyncio.run(run(root))
    print("AstrBot 4.28.2 photo-tool and Airi prefix-mode smoke passed")
