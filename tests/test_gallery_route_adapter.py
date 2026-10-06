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

    @staticmethod
    def on_llm_request(*_args, **_kwargs):
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
    visibility = importlib.import_module(
        f"{PACKAGE_NAME}.services.tool_visibility_adapter"
    )
    return main, adapter, visibility


MAIN_MODULE, ADAPTER_MODULE, VISIBILITY_ADAPTER = _load_modules()


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
        for message in (
            "看看你的自拍",
            "看你的照片",
            "看看林小满",
            "  看看你的照片\t",
            "小满小满我要看看你的照片",
        ):
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

    def test_compositional_intent_families_bypass_airi(self):
        families = {
            "explicit subject plus view/photo": (
                "看看你的照片",
                "我想看看你的自拍",
                "小满给我看一张相片",
                "小满给我，看一张相片",
                "林小满分享一下自己的照片",
                "看看林小满",
            ),
            "implicit subject plus request/receive action": (
                "给我看看照片",
                "想看自拍",
                "能发张照片给我吗",
                "给张自拍",
                "来一张写真",
                "有照片吗",
                "照片有吗",
                "照片呢",
                "自拍可以吗",
                "看看照片",
                "瞅瞅自拍",
                "看一看相片呢",
            ),
            "appearance inquiry": (
                "你长什么样",
                "小满长啥样",
                "能不能告诉我你长什么样",
                "想看看你本人",
                "能让我看看你吗",
                "可以让我看看小满吗",
            ),
            "post-wake-prefix text seen by plugin": (
                "给我看看你的照片",
                "能发张自拍给我吗",
                "想看照片",
            ),
        }
        for family, messages in families.items():
            for message in messages:
                with self.subTest(family=family, message=message):
                    handlers = self._handlers()
                    event = Event(message, directed=True, handlers=handlers)
                    original_text = event.message_str
                    original_message_obj = event.message_obj
                    handlers_id = id(handlers)
                    original_handlers = list(handlers)

                    self.assertTrue(ADAPTER_MODULE.guard_directed_look_request(event))
                    self.assertEqual(id(handlers), handlers_id)
                    self.assertEqual(event.message_str, original_text)
                    self.assertIs(event.message_obj, original_message_obj)
                    self.assertEqual(event.stop_calls, 0)
                    self.assertEqual(
                        [(item.handler_module_path, item.handler_name) for item in handlers],
                        [
                            ("data.plugins.other.main", "before"),
                            (AIRI_MODULE, "cmd_gallery_help"),
                            ("data.plugins.other.main", "handle_gallery_message"),
                        ],
                    )
                    self.assertIs(handlers[0], original_handlers[0])
                    self.assertIs(handlers[1], original_handlers[2])
                    self.assertIs(handlers[2], original_handlers[3])

    def test_false_positive_image_and_unrelated_requests_are_not_intercepted(self):
        families = {
            "user-provided image reference": (
                "帮我看看这张照片",
                "你看看我发的图片",
                "你看看我的自拍",
                "我发给你的照片看到了吗",
                "看看上面那张图",
            ),
            "arbitrary image inspection": (
                "帮我分析一下这张图",
                "看看这个截图是什么意思",
                "看看商品主图",
                "小满看看风景",
                "看看这个文件",
                "帮我识别图片里的文字",
            ),
            "Airi browse/category/range commands": (
                "看看默认",
                "看看风景",
                "看看123",
                "看100-110",
                "看最近",
                "/看默认",
            ),
            "unrelated conversation": (
                "这张照片好看吗",
                "你在干嘛",
                "今天怎么样",
                "你觉得照片重要吗",
            ),
        }
        for family, messages in families.items():
            for message in messages:
                with self.subTest(family=family, message=message):
                    handlers = self._handlers()
                    event = Event(message, directed=True, handlers=handlers)

                    self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(event))
                    self.assertEqual(len(handlers), 4)
                    self.assertEqual(event.message_str, message)

    def test_open_class_competing_targets_and_explicit_self_precedence(self):
        # The first family intentionally uses arbitrary nouns absent from the
        # classifier: noun modifiers are competing visual targets, not a list.
        competing = (
            "给我看看天文台照片",
            "发张猫咪自拍给我",
            "来一张旅行相片",
            "看看朋友的写真",
            "看看用户上传的截图",
        )
        positive = (
            "小满给我看看照片",
            "能不能告訴我你长什么样",
            "给我看看你自己的照片",
            "小满小满，来张自拍",
        )
        for message in competing:
            with self.subTest(kind="competing", message=message):
                self.assertFalse(ADAPTER_MODULE.is_directed_look_request(Event(message, directed=True, handlers=[])))
        for message in positive:
            with self.subTest(kind="self-or-implicit", message=message):
                self.assertTrue(ADAPTER_MODULE.is_directed_look_request(Event(message, directed=True, handlers=[])))

    def test_non_directed_visual_request_is_not_rerouted(self):
        for message in ("给我看看照片", "来一张自拍", "照片呢"):
            with self.subTest(message=message):
                handlers = self._handlers()
                event = Event(message, directed=False, handlers=handlers)

                self.assertFalse(ADAPTER_MODULE.guard_directed_look_request(event))
                self.assertEqual(len(handlers), 4)
                self.assertEqual(event.stop_calls, 0)

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


class ToolVisibilityAdapterTests(unittest.TestCase):
    class ToolSet:
        def __init__(self, names):
            self._names = list(names)
            self.removed = []

        def names(self):
            return list(self._names)

        def remove_tool(self, name):
            self.removed.append(name)
            self._names = [item for item in self._names if item != name]

    def test_hides_only_gallery_send_when_wrapper_is_present(self):
        tool_set = self.ToolSet(
            ["send_xiaoman_photo", "gallery_send", "tts_speak", "other"]
        )
        req = types.SimpleNamespace(func_tool=tool_set)

        self.assertTrue(VISIBILITY_ADAPTER.hide_gallery_tool_for_xiaoman_request(req))
        self.assertEqual(tool_set.removed, ["gallery_send"])
        self.assertEqual(tool_set.names(), ["send_xiaoman_photo", "tts_speak", "other"])

    def test_does_not_add_wrapper_or_hide_gallery_when_wrapper_missing(self):
        tool_set = self.ToolSet(["gallery_send", "tts_speak"])
        req = types.SimpleNamespace(func_tool=tool_set)

        self.assertFalse(VISIBILITY_ADAPTER.hide_gallery_tool_for_xiaoman_request(req))
        self.assertEqual(tool_set.removed, [])
        self.assertEqual(tool_set.names(), ["gallery_send", "tts_speak"])

    def test_missing_or_incompatible_tool_set_fails_open(self):
        for req in (types.SimpleNamespace(func_tool=None), types.SimpleNamespace()):
            with self.subTest(req=req):
                self.assertFalse(VISIBILITY_ADAPTER.hide_gallery_tool_for_xiaoman_request(req))

    def test_hook_is_registered(self):
        self.assertTrue(callable(MAIN_MODULE.Main.hide_delegated_gallery_tool))


if __name__ == "__main__":
    unittest.main()
