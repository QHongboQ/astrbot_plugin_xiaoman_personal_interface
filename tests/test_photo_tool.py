"""Contract tests for the photo Function Tool."""

import asyncio
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


class FunctionToolStub:
    def __init__(self, **kwargs):
        self.name = kwargs["name"]
        self.description = kwargs["description"]
        self.parameters = kwargs["parameters"]


def _install_astrbot_stubs() -> None:
    """Provide only the public API surface used by this small plugin."""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(exception=lambda *_args, **_kwargs: None)
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool = types.ModuleType("astrbot.core.agent.tool")
    tool.FunctionTool = FunctionToolStub
    astrbot.api = api
    astrbot.core = core
    core.agent = agent
    agent.tool = tool
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.tool": tool,
        }
    )


_install_astrbot_stubs()
from tools.photo_tool import XiaomanPhotoTool  # noqa: E402


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

    def get_llm_tool_manager(self):
        return self.tool_manager


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

    def test_delegates_fixed_category_and_count(self):
        gallery_tool = GalleryTool(result="any internal result")
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

    def test_missing_gallery_tool_fails(self):
        manager = ToolManager()
        tool = XiaomanPhotoTool(PluginContext(manager))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")
        self.assertEqual(manager.requested_names, ["gallery_send"])

    def test_gallery_tool_exception_fails(self):
        gallery_tool = GalleryTool(error=RuntimeError("send failed"))
        tool = XiaomanPhotoTool(PluginContext(ToolManager(gallery_tool)))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")

    def test_success_hides_delegated_result(self):
        gallery_tool = GalleryTool(result="private implementation detail")
        tool = XiaomanPhotoTool(PluginContext(ToolManager(gallery_tool)))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SENT")

    def test_never_falls_back_to_a_different_tool_or_category(self):
        manager = ToolManager()
        tool = XiaomanPhotoTool(PluginContext(manager))

        self.assertEqual(self._invoke(tool, object()), "PHOTO_SEND_FAILED")
        self.assertEqual(manager.requested_names, ["gallery_send"])
