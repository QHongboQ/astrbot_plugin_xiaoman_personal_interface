"""Route-guard contracts for Xiaoman-directed no-prefix messages."""

import asyncio
import ast
import importlib
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"
AIRI_MODULE = "data.plugins.astrbot_plugin_airi_gallery.main"


class StarStub:
    def __init__(self, context):
        self.context = context


class FunctionToolStub:
    def __init__(self, **_kwargs):
        pass


class FilterStub:
    EventMessageType = types.SimpleNamespace(ALL="all")
    calls = []

    @staticmethod
    def event_message_type(*_args, **_kwargs):
        FilterStub.calls.append((_args, _kwargs))
        return lambda function: function


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    event = types.ModuleType("astrbot.api.event")
    event.filter = FilterStub
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = StarStub
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    tool.FunctionTool = FunctionToolStub
    astrbot.api = api
    astrbot.core = core
    api.event = event
    api.star = star
    core.agent = agent
    agent.tool = tool
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.tool": tool,
        }
    )


def _install_package_path(package_name: str, path: Path) -> None:
    package = types.ModuleType(package_name)
    package.__path__ = [str(path)]
    package.__package__ = package_name
    sys.modules[package_name] = package


def _load_modules():
    for module_name in list(sys.modules):
        if module_name == PACKAGE_NAME or module_name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[module_name]
    _install_astrbot_stubs()
    _install_package_path("data", PLUGIN_ROOT.parent.parent.parent)
    _install_package_path("data.plugins", PLUGIN_ROOT.parent.parent)
    _install_package_path(PACKAGE_NAME, PLUGIN_ROOT)
    main = importlib.import_module(f"{PACKAGE_NAME}.main")
    adapter = importlib.import_module(f"{PACKAGE_NAME}.services.gallery_route_adapter")
    return main, adapter


MAIN_MODULE, ADAPTER_MODULE = _load_modules()


class Handler:
    def __init__(self, module_path, handler_name):
        self.handler_module_path = module_path
        self.handler_name = handler_name


class Event:
    def __init__(self, message, *, directed, handlers):
        self.message_str = message
        self.is_at_or_wake_command = directed
        self.message_obj = object()
        self._extras = {"activated_handlers": handlers}
        self.stop_calls = 0

    def get_extra(self, key):
        return self._extras[key]

    def stop_event(self):
        self.stop_calls += 1


def _airi_generic_handler():
    return Handler(AIRI_MODULE, "handle_gallery_message")


class GalleryRouteAdapterTests(unittest.TestCase):
    def _handlers(self):
        return [
            Handler("data.plugins.other.main", "before"),
            _airi_generic_handler(),
            Handler(AIRI_MODULE, "cmd_gallery_help"),
            Handler("data.plugins.other.main", "handle_gallery_message"),
        ]

    def test_directed_look_removes_only_airi_generic_handler_in_place(self):
        handlers = self._handlers()
        handlers_id = id(handlers)
        event = Event("看看你的照片", directed=True, handlers=handlers)
        original_text = event.message_str
        original_components = event.message_obj

        changed = ADAPTER_MODULE.guard_directed_look_request(event)

        self.assertTrue(changed)
        self.assertEqual(id(event.get_extra("activated_handlers")), handlers_id)
        self.assertNotIn("handle_gallery_message", [
            handler.handler_name
            for handler in handlers
            if handler.handler_module_path == AIRI_MODULE
        ])
        self.assertEqual([handler.handler_name for handler in handlers], [
            "before",
            "cmd_gallery_help",
            "handle_gallery_message",
        ])
        self.assertEqual(event.message_str, original_text)
        self.assertIs(event.message_obj, original_components)
        self.assertEqual(event.stop_calls, 0)

    def test_guard_is_registered_before_airi_generic_priority(self):
        self.assertIn((("all",), {"priority": 100}), FilterStub.calls)

    def test_all_directed_look_forms_bypass_airi(self):
        for message in ("看看你的自拍", "看你的照片", "看看林小满", "  看看你的照片\t"):
            with self.subTest(message=message):
                handlers = self._handlers()
                event = Event(message, directed=True, handlers=handlers)

                self.assertTrue(ADAPTER_MODULE.guard_directed_look_request(event))
                self.assertFalse(any(handler is not None and handler.handler_module_path == AIRI_MODULE and handler.handler_name == "handle_gallery_message" for handler in handlers))

    def test_non_directed_airi_commands_remain_active(self):
        for message in ("看看林小满", "看看默认", "看看123", "看100-110"):
            with self.subTest(message=message):
                handlers = self._handlers()
                event = Event(message, directed=False, handlers=handlers)

                self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(event))
                self.assertTrue(any(handler.handler_module_path == AIRI_MODULE and handler.handler_name == "handle_gallery_message" for handler in handlers))

    def test_unrelated_directed_message_leaves_handlers_untouched(self):
        handlers = self._handlers()
        event = Event("你在干嘛", directed=True, handlers=handlers)

        self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(event))
        self.assertEqual(len(handlers), 4)

    def test_failure_to_read_handlers_fails_open(self):
        class BrokenEvent:
            is_at_or_wake_command = True
            message_str = "看看你的照片"

            @staticmethod
            def get_extra(_key):
                raise RuntimeError("missing")

        self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(BrokenEvent()))

    def test_non_list_handler_container_fails_open(self):
        handlers = tuple(self._handlers())
        event = Event("看看你的照片", directed=True, handlers=handlers)

        self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(event))
        self.assertEqual(len(handlers), 4)

    def test_main_handler_applies_the_same_guard(self):
        handlers = self._handlers()
        event = Event("看看你的照片", directed=True, handlers=handlers)

        asyncio.run(MAIN_MODULE.Main(types.SimpleNamespace(add_llm_tools=lambda *_: None)).guard_directed_gallery_route(event))

        self.assertFalse(any(handler.handler_module_path == AIRI_MODULE and handler.handler_name == "handle_gallery_message" for handler in handlers))
        self.assertEqual(event.stop_calls, 0)

    def test_adapter_has_no_airi_import_or_monkey_patch(self):
        source = (PLUGIN_ROOT / "services" / "gallery_route_adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]

        self.assertFalse(any("astrbot_plugin_airi_gallery" in name for name in imported_modules))
        self.assertNotIn("monkey", source.lower())
        self.assertNotIn("stop_event", source)


if __name__ == "__main__":
    unittest.main()
