"""Tests for tts_speak-only Xiaoman voice adaptation."""

import asyncio
import importlib
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "data.plugins.astrbot_plugin_xiaoman_personal_interface"


class FunctionToolStub:
    def __init__(self, **kwargs):
        self.name = kwargs["name"]
        self.description = kwargs["description"]
        self.parameters = kwargs["parameters"]


class StarStub:
    def __init__(self, context):
        self.context = context


class FilterStub:
    @staticmethod
    def on_using_llm_tool(*_args, **_kwargs):
        return lambda function: function


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(exception=lambda *_args, **_kwargs: None)
    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = FilterStub
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


def _load_plugin_modules():
    for module_name in list(sys.modules):
        if module_name == PACKAGE_NAME or module_name.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[module_name]

    _install_package_path("data", PLUGIN_ROOT.parent.parent.parent)
    _install_package_path("data.plugins", PLUGIN_ROOT.parent.parent)
    _install_package_path(PACKAGE_NAME, PLUGIN_ROOT)
    main = importlib.import_module(f"{PACKAGE_NAME}.main")
    adapter = importlib.import_module(f"{PACKAGE_NAME}.services.voice_tool_adapter")
    return main, adapter


_install_astrbot_stubs()
MAIN_MODULE, ADAPTER_MODULE = _load_plugin_modules()


class PluginContext:
    def add_llm_tools(self, *_tools):
        return None


class Tool:
    def __init__(self, name):
        self.name = name


class VoiceToolAdapterTests(unittest.TestCase):
    def _call_hook(self, tool_name, tool_args):
        plugin = MAIN_MODULE.Main(PluginContext())
        asyncio.run(plugin.on_using_llm_tool(object(), Tool(tool_name), tool_args))

    def test_plugin_has_no_global_llm_request_hook(self):
        self.assertFalse(hasattr(MAIN_MODULE.Main, "on_llm_request"))

    def test_adapter_introduces_no_llm_request_or_model_call(self):
        source = inspect.getsource(MAIN_MODULE) + inspect.getsource(ADAPTER_MODULE)

        self.assertNotIn("on_llm_request", source)
        self.assertNotIn("text_chat", source)

    def test_non_tts_tool_is_ignored(self):
        args = {"text": "你还真信了啊？笨死了"}

        self._call_hook("send_xiaoman_photo", args)

        self.assertEqual(args["text"], "你还真信了啊？笨死了")

    def test_plain_tts_speak_gets_baseline_tag(self):
        args = {"text": "你还真信了啊？笨死了"}

        self._call_hook("tts_speak", args)

        self.assertEqual(
            args["text"],
            "（语速稍快，连续说，停顿很短）你还真信了啊？笨死了",
        )

    def test_existing_full_width_tag_remains_unchanged(self):
        args = {"text": "（轻笑）你还真信了啊？"}

        self._call_hook("tts_speak", args)

        self.assertEqual(args["text"], "（轻笑）你还真信了啊？")

    def test_existing_baseline_tag_remains_unchanged(self):
        args = {"text": "（语速稍快，连续说，停顿很短）已经很好了"}

        self._call_hook("tts_speak", args)

        self.assertEqual(args["text"], "（语速稍快，连续说，停顿很短）已经很好了")

    def test_ascii_parentheses_tag_is_recognized(self):
        args = {"text": "(心虚)我才没有呢"}

        self._call_hook("tts_speak", args)

        self.assertEqual(args["text"], "(心虚)我才没有呢")

    def test_empty_or_missing_text_is_unchanged(self):
        empty_args = {"text": ""}
        missing_args = {}

        self._call_hook("tts_speak", empty_args)
        self._call_hook("tts_speak", missing_args)

        self.assertEqual(empty_args, {"text": ""})
        self.assertEqual(missing_args, {})

    def test_none_tool_args_does_not_crash(self):
        self._call_hook("tts_speak", None)

    def test_adapter_error_does_not_prevent_tool_flow(self):
        args = {"text": "原文"}
        plugin = MAIN_MODULE.Main(PluginContext())

        with patch.object(MAIN_MODULE, "decorate_tts_speak_tool_args", side_effect=RuntimeError):
            asyncio.run(plugin.on_using_llm_tool(object(), Tool("tts_speak"), args))

        self.assertEqual(args, {"text": "原文"})

    def test_adapter_does_not_import_tts_plugin_internals(self):
        source = inspect.getsource(ADAPTER_MODULE)
        self.assertNotIn("tts_emotion_router", source)
