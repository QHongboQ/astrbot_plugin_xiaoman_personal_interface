"""Plugin-loading and delegation contracts for 林小满个人接口."""

import asyncio
import importlib
import inspect
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"
SUCCESS_RESULT = "已从 林小满 分类发送 1 张图片。"


class FunctionToolStub:
    def __init__(self, **kwargs):
        self.name = kwargs["name"]
        self.description = kwargs["description"]
        self.parameters = kwargs["parameters"]


class StarStub:
    def __init__(self, context):
        self.context = context


class FilterStub:
    class PermissionType:
        ADMIN = "admin"

    class EventMessageType:
        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2

    @staticmethod
    def _decorator(*_args, **_kwargs):
        return lambda function: function

    @staticmethod
    def on_llm_request(*_args, **_kwargs):
        return lambda function: function

    permission_type = _decorator
    command = _decorator
    event_message_type = _decorator


def _install_astrbot_stubs() -> None:
    """Provide only the AstrBot public API surface used by this plugin."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        exception=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
    )
    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = FilterStub
    api_event.EventMessageType = FilterStub.EventMessageType
    api_event.PermissionType = FilterStub.PermissionType
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = object
    api_star.Star = StarStub
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    tool.FunctionTool = FunctionToolStub
    astrbot.api = api
    astrbot.core = core
    api.event = api_event
    api.star = api_star
    core.agent = agent
    agent.tool = tool
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.api.star": api_star,
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


def _load_plugin_as_astrbot_package():
    """Load the plugin under the namespace assigned by AstrBot discovery."""
    for module_name in list(sys.modules):
        if module_name == PACKAGE_NAME or module_name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[module_name]

    _install_package_path("data", PLUGIN_ROOT.parent.parent.parent)
    _install_package_path("data.plugins", PLUGIN_ROOT.parent.parent)
    _install_package_path(PACKAGE_NAME, PLUGIN_ROOT)
    return importlib.import_module(f"{PACKAGE_NAME}.main")


_install_astrbot_stubs()
MAIN_MODULE = _load_plugin_as_astrbot_package()
PHOTO_TOOL_MODULE = importlib.import_module(f"{PACKAGE_NAME}.tools.photo_tool")
XiaomanPhotoTool = PHOTO_TOOL_MODULE.XiaomanPhotoTool


class GalleryTool:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def call(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class ToolManager:
    def __init__(self, gallery_tool=None):
        self.gallery_tool = gallery_tool
        self.requested_names = []

    def get_func(self, name):
        self.requested_names.append(name)
        if name == "gallery_send":
            return self.gallery_tool
        raise AssertionError("unexpected tool lookup")


class PluginContext:
    def __init__(self, tool_manager):
        self.tool_manager = tool_manager
        self.registered_tools = []

    def get_llm_tool_manager(self):
        return self.tool_manager

    def add_llm_tools(self, *tools):
        self.registered_tools.extend(tools)


class PluginLoadTests(unittest.TestCase):
    def test_plugin_package_imports_successfully(self):
        self.assertEqual(MAIN_MODULE.__name__, f"{PACKAGE_NAME}.main")

    def test_main_exists_and_subclasses_star(self):
        self.assertTrue(hasattr(MAIN_MODULE, "Main"))
        self.assertTrue(issubclass(MAIN_MODULE.Main, StarStub))

    def test_internal_imports_are_package_relative(self):
        self.assertEqual(PHOTO_TOOL_MODULE.__name__, f"{PACKAGE_NAME}.tools.photo_tool")
        self.assertIn(f"{PACKAGE_NAME}.services.photo_service", sys.modules)
        self.assertNotIn("tools.photo_tool", sys.modules)
        self.assertNotIn("services.photo_service", sys.modules)

    def test_registered_tool_has_plugin_module_path(self):
        context = PluginContext(ToolManager())
        MAIN_MODULE.Main(context)

        self.assertEqual(len(context.registered_tools), 1)
        self.assertEqual(
            type(context.registered_tools[0]).__module__,
            f"{PACKAGE_NAME}.tools.photo_tool",
        )

    def test_main_registers_photo_tool_and_request_local_visibility_hook(self):
        context = PluginContext(ToolManager())
        plugin = MAIN_MODULE.Main(context)
        source = inspect.getsource(MAIN_MODULE.Main)

        self.assertEqual([tool.name for tool in context.registered_tools], ["send_xiaoman_photo"])
        self.assertFalse(hasattr(plugin, "on_using_llm_tool"))
        self.assertIn("on_llm_request", source)
        self.assertNotIn("on_using_llm_tool", source)

    def test_no_natural_language_routing_hook_or_message_workaround_remains(self):
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        for removed_route_marker in (
            "guard_directed_gallery_route",
            "gallery_route_adapter",
            "stop_event",
        ):
            self.assertNotIn(removed_route_marker, source)

        for path in (PLUGIN_ROOT / "services").glob("*.py"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("stop_event", content)

    def test_request_local_visibility_hides_only_generic_gallery_tool(self):
        class ToolSetStub:
            def __init__(self, names):
                self._names = list(names)

            def names(self):
                return list(self._names)

            def remove_tool(self, name):
                self._names.remove(name)

        req = types.SimpleNamespace(
            func_tool=ToolSetStub(["send_xiaoman_photo", "gallery_send", "tts_speak"])
        )
        plugin = MAIN_MODULE.Main(PluginContext(ToolManager()))

        asyncio.run(plugin.hide_delegated_gallery_tool(None, req))

        self.assertEqual(req.func_tool.names(), ["send_xiaoman_photo", "tts_speak"])

    def test_runtime_source_has_no_voice_or_mimo_logic(self):
        runtime_sources = [
            (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"),
            *(path.read_text(encoding="utf-8") for path in (PLUGIN_ROOT / "services").glob("*.py")),
            *(path.read_text(encoding="utf-8") for path in (PLUGIN_ROOT / "tools").glob("*.py")),
        ]
        source = "\n".join(runtime_sources).lower()

        for forbidden in (
            "tts_speak",
            "mimo",
            "voice_tool",
            "语速稍快",
            "轻笑",
            "心虚",
            "委屈",
            "不耐烦",
            "疲惫",
        ):
            self.assertNotIn(forbidden, source)


class XiaomanPhotoToolTests(unittest.TestCase):
    def _invoke(self, tool, current_context):
        return asyncio.run(tool.call(current_context))

    def test_has_no_argument_schema(self):
        tool = XiaomanPhotoTool(PluginContext(ToolManager()))

        self.assertEqual(tool.name, "send_xiaoman_photo")
        self.assertEqual(
            tool.parameters,
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        self.assertIn("照片", tool.description)
        self.assertIn("若决定不发送，则正常回复而不要调用", tool.description)

    def test_gallery_success_returns_photo_sent(self):
        gallery_tool = GalleryTool(result=SUCCESS_RESULT)
        manager = ToolManager(gallery_tool)
        tool = XiaomanPhotoTool(PluginContext(manager))
        current_context = object()

        result = self._invoke(tool, current_context)

        self.assertEqual(result, "PHOTO_SENT")
        self.assertEqual(manager.requested_names, ["gallery_send"])
        self.assertEqual(
            gallery_tool.calls,
            [(current_context, {"category": "林小满", "count": 1})],
        )

    def test_gallery_success_tolerates_spacing_and_optional_punctuation(self):
        for result_text in (
            "已从林小满分类发送1张图片",
            "  已从  林小满 分类发送 1 张图片！  ",
        ):
            with self.subTest(result_text=result_text):
                tool = XiaomanPhotoTool(
                    PluginContext(ToolManager(GalleryTool(result=result_text)))
                )
                self.assertEqual(self._invoke(tool, object()), "PHOTO_SENT")

    def test_gallery_normal_failure_result_returns_failed(self):
        gallery_tool = GalleryTool(result="图库分类 林小满 中没有可用的图片。")
        tool = XiaomanPhotoTool(PluginContext(ToolManager(gallery_tool)))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")

    def test_gallery_exception_returns_failed(self):
        gallery_tool = GalleryTool(error=RuntimeError("send failed"))
        tool = XiaomanPhotoTool(PluginContext(ToolManager(gallery_tool)))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")

    def test_missing_gallery_tool_returns_failed(self):
        manager = ToolManager()
        tool = XiaomanPhotoTool(PluginContext(manager))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")
        self.assertEqual(manager.requested_names, ["gallery_send"])

    def test_never_falls_back_to_another_category(self):
        gallery_tool = GalleryTool(result="图库分类 林小满 中没有可用的图片。")
        manager = ToolManager(gallery_tool)
        tool = XiaomanPhotoTool(PluginContext(manager))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")
        self.assertEqual(manager.requested_names, ["gallery_send"])
        self.assertEqual(len(gallery_tool.calls), 1)
        self.assertEqual(
            gallery_tool.calls[0][1],
            {"category": "林小满", "count": 1},
        )
