"""Tests for stable Xiaoman voice-performance request guidance."""

import asyncio
import importlib
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
    def on_llm_request(*_args, **_kwargs):
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
    guidance = importlib.import_module(f"{PACKAGE_NAME}.services.voice_guidance")
    return main, guidance


_install_astrbot_stubs()
MAIN_MODULE, GUIDANCE_MODULE = _load_plugin_modules()


class PluginContext:
    def add_llm_tools(self, *_tools):
        return None


class Request:
    def __init__(self, system_prompt=""):
        self.system_prompt = system_prompt


class VoiceGuidanceTests(unittest.TestCase):
    def _run_hook(self, system_prompt=""):
        plugin = MAIN_MODULE.Main(PluginContext())
        request = Request(system_prompt)
        asyncio.run(plugin.on_llm_request(object(), request))
        return request

    def test_hook_appends_guidance_after_original_system_prompt(self):
        request = self._run_hook("ORIGINAL SYSTEM PROMPT")

        self.assertTrue(request.system_prompt.startswith("ORIGINAL SYSTEM PROMPT\n\n"))
        self.assertIn(GUIDANCE_MODULE.GUIDANCE_MARKER, request.system_prompt)

    def test_original_system_prompt_is_preserved(self):
        original = "规则 A\n规则 B"

        self.assertIn(original, self._run_hook(original).system_prompt)

    def test_same_request_does_not_duplicate_guidance(self):
        plugin = MAIN_MODULE.Main(PluginContext())
        request = Request("original")

        asyncio.run(plugin.on_llm_request(object(), request))
        asyncio.run(plugin.on_llm_request(object(), request))

        self.assertEqual(request.system_prompt.count(GUIDANCE_MODULE.GUIDANCE_MARKER), 1)

    def test_existing_guidance_marker_is_not_appended_again(self):
        original = f"original\n{GUIDANCE_MODULE.GUIDANCE_MARKER}\nexisting"

        self.assertEqual(self._run_hook(original).system_prompt, original)

    def test_empty_system_prompt_is_supported(self):
        request = self._run_hook()

        self.assertEqual(request.system_prompt, GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE)

    def test_guidance_contains_required_stable_rules(self):
        guidance = GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE

        for required_text in (
            "自然略快",
            "连续",
            "短停顿",
            "0～1 个最常见",
            "最多使用 2 个标签",
            "轻笑",
            "心虚",
            "委屈",
            "稍慢，语气放软",
            "放慢一点，重点说",
        ):
            self.assertIn(required_text, guidance)

    def test_guidance_explicitly_forbids_tag_stacking(self):
        self.assertIn("不要连续堆叠多个标签", GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE)

    def test_hook_exception_preserves_normal_request_flow(self):
        request = Request("untouched")
        plugin = MAIN_MODULE.Main(PluginContext())

        with patch.object(MAIN_MODULE, "append_voice_guidance", side_effect=RuntimeError):
            asyncio.run(plugin.on_llm_request(object(), request))

        self.assertEqual(request.system_prompt, "untouched")
