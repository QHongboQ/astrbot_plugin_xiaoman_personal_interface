"""Contracts for UMO-scoped administrator test mode and AngelHeart isolation."""

from __future__ import annotations

import asyncio
import ast
import types
import unittest

from test_photo_tool import MAIN_MODULE, PluginContext, ToolManager
from data.plugins.astrbot_plugin_xiaoman_personal_interface.services.test_bypass import (
    ANGELHEART_HANDLER_NAME,
    ANGELHEART_PLUGIN_NAME,
    TEST_ADMIN_UID,
    TEST_GUIDANCE,
    apply_admin_test_bypass,
    inject_test_guidance,
    update_test_mode,
)


class Handler:
    def __init__(self, module=ANGELHEART_PLUGIN_NAME + ".main", name=ANGELHEART_HANDLER_NAME):
        self.handler_module_path = module
        self.handler_name = name


class Event:
    def __init__(
        self,
        uid=TEST_ADMIN_UID,
        umo="qq:group:123",
        plugins_name=None,
        message="小满小满，给我看看你的照片",
        handlers=None,
    ):
        self.uid = uid
        self.unified_msg_origin = umo
        self.plugins_name = plugins_name
        self.message_str = message
        self.is_at_or_wake_command = False
        self.extras = {"activated_handlers": handlers or []}
        self.stop_calls = 0

    def get_sender_id(self):
        return self.uid

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def stop_event(self):
        self.stop_calls += 1

    def plain_result(self, text):
        return text


class PluginContext(PluginContext):
    def __init__(self, tool_manager=None, stars=None, fail=False):
        super().__init__(tool_manager or ToolManager())
        self.stars = stars or []
        self.fail = fail

    def get_all_stars(self):
        if self.fail:
            raise RuntimeError("unavailable")
        return self.stars


def handler_set():
    return [
        Handler(),
        Handler(module="data.plugins.other_plugin.main", name=ANGELHEART_HANDLER_NAME),
        Handler(module="data.plugins.astrbot_plugin_angel_heart.main", name="other_handler"),
        Handler(module="data.plugins.xiaoman.main", name="normal_handler"),
    ]


class TestBypassTests(unittest.TestCase):
    def _apply(self, event, stars=None, context=None):
        return apply_admin_test_bypass(
            event,
            {"qq:group:123"},
            context or PluginContext(stars=stars),
        )

    def test_mode_off_leaves_event_unchanged(self):
        event = Event(plugins_name=["other", ANGELHEART_PLUGIN_NAME], handlers=handler_set())
        handlers = event.extras["activated_handlers"]
        before = list(handlers)
        self.assertFalse(apply_admin_test_bypass(event, set(), PluginContext()))
        self.assertIs(event.extras["activated_handlers"], handlers)
        self.assertEqual(handlers, before)
        self.assertEqual(event.plugins_name, ["other", ANGELHEART_PLUGIN_NAME])
        self.assertFalse(event.is_at_or_wake_command)

    def test_wrong_uid_leaves_event_unchanged(self):
        event = Event(uid="123", plugins_name=None, handlers=handler_set())
        before = list(event.extras["activated_handlers"])
        self.assertFalse(self._apply(event))
        self.assertEqual(event.extras["activated_handlers"], before)
        self.assertIsNone(event.plugins_name)

    def test_wrong_umo_leaves_event_unchanged(self):
        event = Event(umo="qq:group:other", plugins_name=None, handlers=handler_set())
        before = list(event.extras["activated_handlers"])
        self.assertFalse(self._apply(event))
        self.assertEqual(event.extras["activated_handlers"], before)
        self.assertIsNone(event.plugins_name)

    def test_enabled_admin_gets_full_bypass_in_place(self):
        event = Event(handlers=handler_set())
        original_handlers = event.extras["activated_handlers"]
        message = event.message_str
        self.assertTrue(self._apply(event))
        self.assertIs(event.extras["activated_handlers"], original_handlers)
        self.assertEqual(len(original_handlers), 3)
        self.assertEqual(original_handlers[0].handler_module_path, "data.plugins.other_plugin.main")
        self.assertEqual(event.plugins_name, [])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, message)
        self.assertEqual(event.stop_calls, 0)

    def test_explicit_plugin_subset_is_preserved_minus_angelheart(self):
        event = Event(plugins_name=["other", ANGELHEART_PLUGIN_NAME, "third"])
        self.assertTrue(self._apply(event))
        self.assertEqual(event.plugins_name, ["other", "third"])

    def test_none_plugin_set_uses_active_plugin_names(self):
        stars = [
            types.SimpleNamespace(name="one", activated=True),
            types.SimpleNamespace(name=ANGELHEART_PLUGIN_NAME, activated=True),
            types.SimpleNamespace(name="two", activated=True),
        ]
        event = Event(plugins_name=None)
        self.assertTrue(self._apply(event, stars=stars))
        self.assertEqual(event.plugins_name, ["one", "two"])

    def test_inactive_plugin_metadata_is_not_added(self):
        stars = [
            types.SimpleNamespace(name="active", activated=True),
            types.SimpleNamespace(name="inactive", activated=False),
            types.SimpleNamespace(name=ANGELHEART_PLUGIN_NAME, activated=True),
        ]
        event = Event(plugins_name=None)
        self.assertTrue(self._apply(event, stars=stars))
        self.assertEqual(event.plugins_name, ["active"])

    def test_get_all_stars_unavailable_or_raises_fails_open_completely(self):
        for context in (types.SimpleNamespace(), PluginContext(fail=True)):
            with self.subTest(context=context):
                event = Event(plugins_name=None, handlers=handler_set())
                handlers = event.extras["activated_handlers"]
                before = list(handlers)
                self.assertFalse(self._apply(event, context=context))
                self.assertIs(event.extras["activated_handlers"], handlers)
                self.assertEqual(handlers, before)
                self.assertIsNone(event.plugins_name)
                self.assertFalse(event.is_at_or_wake_command)

    def test_uninspectable_handler_fails_open_before_any_mutation(self):
        malformed_handler = object()
        event = Event(plugins_name=[ANGELHEART_PLUGIN_NAME], handlers=[malformed_handler])
        handlers = event.extras["activated_handlers"]
        self.assertFalse(self._apply(event))
        self.assertIs(event.extras["activated_handlers"], handlers)
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers[0], malformed_handler)
        self.assertEqual(event.plugins_name, [ANGELHEART_PLUGIN_NAME])
        self.assertFalse(event.is_at_or_wake_command)

    def test_uninspectable_active_plugin_metadata_fails_open(self):
        event = Event(plugins_name=None, handlers=handler_set())
        handlers = event.extras["activated_handlers"]
        before = list(handlers)
        context = PluginContext(stars=[object()])
        self.assertFalse(self._apply(event, context=context))
        self.assertIs(event.extras["activated_handlers"], handlers)
        self.assertEqual(handlers, before)
        self.assertIsNone(event.plugins_name)
        self.assertFalse(event.is_at_or_wake_command)

    def test_test_command_event_is_not_bypassed(self):
        event = Event(message="/xiaoman_test on", plugins_name=[ANGELHEART_PLUGIN_NAME], handlers=handler_set())
        before = list(event.extras["activated_handlers"])
        self.assertFalse(self._apply(event))
        self.assertEqual(event.extras["activated_handlers"], before)
        self.assertEqual(event.plugins_name, [ANGELHEART_PLUGIN_NAME])

    def test_guidance_is_appended_once_without_mutating_prompts_or_message(self):
        event = Event()
        req = types.SimpleNamespace(
            extra_user_content_parts=[],
            system_prompt="system",
            prompt="prompt",
        )
        message = event.message_str
        self.assertTrue(inject_test_guidance(event, {event.unified_msg_origin}, req))
        self.assertFalse(inject_test_guidance(event, {event.unified_msg_origin}, req))
        self.assertEqual(req.extra_user_content_parts, [TEST_GUIDANCE])
        self.assertEqual(req.system_prompt, "system")
        self.assertEqual(req.prompt, "prompt")
        self.assertEqual(event.message_str, message)
        self.assertIn("小满小满，给我看看你的照片", message)

    def test_command_on_off_status_responses(self):
        state = set()
        event = Event()
        self.assertEqual(update_test_mode(event, state, "status"), "小满测试放行模式：已关闭")
        self.assertEqual(update_test_mode(event, state, "on"), "小满测试放行模式：已开启（仅当前会话）")
        self.assertEqual(update_test_mode(event, state, "status"), "小满测试放行模式：已开启")
        self.assertEqual(update_test_mode(event, state, "off"), "小满测试放行模式：已关闭")
        self.assertEqual(state, set())

    def test_unauthorized_uid_cannot_change_state(self):
        state = set()
        event = Event(uid="another-admin")
        self.assertEqual(update_test_mode(event, state, "on"), "无权限")
        self.assertEqual(state, set())

    def test_umo_state_isolation(self):
        state = set()
        first = Event(umo="qq:group:first")
        second = Event(umo="qq:group:second")
        update_test_mode(first, state, "on")
        self.assertIn(first.unified_msg_origin, state)
        self.assertNotIn(second.unified_msg_origin, state)

    def test_fresh_main_starts_with_empty_mode_state(self):
        plugin = MAIN_MODULE.Main(PluginContext())
        self.assertEqual(plugin._test_mode_umos, set())

    def test_plugin_command_uses_admin_permission_and_command_name(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        command = methods["xiaoman_test"]
        decorator_names = [
            ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator)
            for decorator in command.decorator_list
        ]
        self.assertEqual(
            decorator_names,
            ["filter.permission_type", "filter.command"],
        )
        self.assertEqual(command.decorator_list[1].args[0].value, "xiaoman_test")

    def test_exactly_one_priority_100_test_bypass_hook_is_registered(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
        hooks = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                name = ast.unparse(decorator.func)
                if name == "filter.event_message_type":
                    priority = next(
                        (
                            keyword.value.value
                            for keyword in decorator.keywords
                            if keyword.arg == "priority"
                            and isinstance(keyword.value, ast.Constant)
                        ),
                        None,
                    )
                    hooks.append((node.name, priority))
        self.assertEqual(hooks, [("bypass_angelheart_for_test_event", 100)])

    def test_no_old_natural_language_gallery_routing_returns(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "services" / "gallery_route_adapter.py").exists())
        self.assertFalse((root / "tests" / "test_gallery_route_adapter.py").exists())
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / "main.py", *(root / "services").glob("*.py"))
        )
        for marker in (
            "guard_directed_gallery_route",
            "guard_directed_look_request",
            'startswith("看")',
        ):
            self.assertNotIn(marker, source)


class TestModeCommandHandlerTests(unittest.TestCase):
    def test_command_handler_yields_expected_response(self):
        plugin = MAIN_MODULE.Main(PluginContext())
        event = Event()

        async def collect():
            return [item async for item in plugin.xiaoman_test(event, "on")]

        self.assertEqual(asyncio.run(collect()), ["小满测试放行模式：已开启（仅当前会话）"])
        self.assertEqual(plugin._test_mode_umos, {event.unified_msg_origin})


if __name__ == "__main__":
    unittest.main()
