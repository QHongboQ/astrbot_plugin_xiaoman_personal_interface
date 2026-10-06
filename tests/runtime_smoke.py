"""Minimal AstrBot v4.28.2 runtime smoke for the clean photo-only plugin."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import sys
import types
from pathlib import Path
import re

from astrbot.api.star import Star


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

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)


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


async def run(official_airi_root: Path | None = None) -> None:
    assert importlib.metadata.version("AstrBot") == "4.28.2"
    main = load_main()
    context = Context()
    plugin = main.Main(context)
    assert isinstance(plugin, Star)
    assert [tool.name for tool in context.tools] == ["send_xiaoman_photo"]

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


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else None
    asyncio.run(run(root))
    print("AstrBot 4.28.2 Xiaoman route-guard runtime smoke passed")
