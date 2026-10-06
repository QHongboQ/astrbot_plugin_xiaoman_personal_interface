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


class ToolManager:
    def __init__(self, tts_tool=None, error=None):
        self.tts_tool = tts_tool
        self.error = error
        self.requested_names = []

    def get_func(self, name):
        self.requested_names.append(name)
        if self.error is not None:
            raise self.error
        if name == "tts_speak":
            return self.tts_tool
        return None


class PluginContext:
    def __init__(self, tool_manager=None):
        self.tool_manager = tool_manager or ToolManager(tts_tool=object())
        self.registered_tools = []

    def add_llm_tools(self, *tools):
        self.registered_tools.extend(tools)

    def get_llm_tool_manager(self):
        return self.tool_manager


class Request:
    def __init__(self, system_prompt=""):
        self.system_prompt = system_prompt


class VoiceGuidanceTests(unittest.TestCase):
    def _run_hook(self, system_prompt="", *, tool_manager=None):
        plugin = MAIN_MODULE.Main(PluginContext(tool_manager))
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

    def test_guidance_is_skipped_when_tts_speak_is_missing(self):
        manager = ToolManager(tts_tool=None)
        request = self._run_hook("original", tool_manager=manager)

        self.assertEqual(request.system_prompt, "original")
        self.assertEqual(manager.requested_names, ["tts_speak"])

    def test_guidance_is_skipped_when_tool_manager_lookup_fails(self):
        manager = ToolManager(error=RuntimeError("tool lookup failed"))
        request = self._run_hook("original", tool_manager=manager)

        self.assertEqual(request.system_prompt, "original")

    def test_guidance_contains_preferred_baseline_tag(self):
        baseline = "语速稍快，连续说，停顿很短"

        self.assertEqual(GUIDANCE_MODULE.BASELINE_PERFORMANCE_TAG, baseline)
        self.assertEqual(GUIDANCE_MODULE.SUPPORTED_PERFORMANCE_TAGS[0], baseline)
        self.assertIn(f"（{baseline}）", GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE)

    def test_supported_tags_are_explicit_and_stable(self):
        self.assertEqual(
            GUIDANCE_MODULE.SUPPORTED_PERFORMANCE_TAGS,
            (
                "语速稍快，连续说，停顿很短",
                "轻笑",
                "心虚",
                "委屈",
                "不耐烦",
                "疲惫",
                "撒娇",
                "气声",
                "鼻音",
                "稍慢，语气放软",
                "放慢一点，重点说",
            ),
        )

    def test_guidance_requires_only_whitelisted_tags(self):
        guidance = GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE

        self.assertIn("只能使用上面的白名单标签", guidance)
        self.assertIn("不要发明、改写或组合新的括号控制词", guidance)

    def test_guidance_limits_short_replies_to_two_tags(self):
        guidance = GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE

        self.assertIn("普通短回复总数最多 2 个", guidance)
        self.assertIn("不要连续堆叠多个标签", guidance)

    def test_guidance_contains_local_slowing_rules(self):
        guidance = GUIDANCE_MODULE.XIAOMAN_MIMO_VOICE_GUIDANCE

        self.assertIn("稍慢，语气放软", guidance)
        self.assertIn("放慢一点，重点说", guidance)
        self.assertIn("不要让整段都变慢", guidance)

    def test_helper_detects_registered_tts_speak(self):
        context = PluginContext(ToolManager(tts_tool=object()))

        self.assertTrue(GUIDANCE_MODULE.is_tts_speak_available(context))

    def test_helper_returns_false_for_missing_tts_speak(self):
        context = PluginContext(ToolManager(tts_tool=None))

        self.assertFalse(GUIDANCE_MODULE.is_tts_speak_available(context))

    def test_hook_exception_preserves_normal_request_flow(self):
        request = Request("untouched")
        plugin = MAIN_MODULE.Main(PluginContext())

        with patch.object(MAIN_MODULE, "append_voice_guidance", side_effect=RuntimeError):
            asyncio.run(plugin.on_llm_request(object(), request))

        self.assertEqual(request.system_prompt, "untouched")


if __name__ == "__main__":
    unittest.main()
