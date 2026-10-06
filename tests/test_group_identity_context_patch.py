"""Tests for the QQ group identity context compatibility patch."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "services" / "group_identity_context_patch.py"
SPEC = importlib.util.spec_from_file_location("xiaoman_group_identity_patch_test", MODULE_PATH)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PATCH)


class Sender:
    def __init__(self, nickname: str, user_id: str = "") -> None:
        self.nickname = nickname
        self.user_id = user_id


class MessageObj:
    def __init__(self, nickname: str, group_id: str, user_id: str = "") -> None:
        self.sender = Sender(nickname, user_id)
        self.group_id = group_id


class Event:
    def __init__(
        self,
        nickname: str,
        sender_id: str,
        group_id: str,
    ) -> None:
        self.message_obj = MessageObj(nickname, group_id, sender_id)
        self._sender_id = sender_id
        self._group_id = group_id

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id


class GroupIdentityRewriteTests(unittest.TestCase):
    def test_explicitly_labels_nickname_qq_group_and_time(self):
        event = Event("白丝少妇", "2409043649", "1153387215")
        rendered = "[白丝少妇/22:35:01]:  你好"

        rewritten = PATCH.rewrite_group_context_record(rendered, event)

        self.assertEqual(
            rewritten,
            "[昵称：白丝少妇 | QQ号：2409043649 | 群号：1153387215 | 时间：22:35:01]：  你好",
        )

    def test_system_notice_like_nickname_does_not_make_group_id_ambiguous(self):
        event = Event(
            "群主邀请了“白丝少妇”加入了群聊",
            "2409043649",
            "1439184369",
        )
        rendered = "[群主邀请了“白丝少妇”加入了群聊/14:39:18]:  欢迎加入"

        rewritten = PATCH.rewrite_group_context_record(rendered, event)

        self.assertIn("昵称：群主邀请了“白丝少妇”加入了群聊", rewritten)
        self.assertIn("QQ号：2409043649", rewritten)
        self.assertIn("群号：1439184369", rewritten)
        self.assertIn("时间：14:39:18", rewritten)
        self.assertTrue(rewritten.endswith("欢迎加入"))

    def test_unknown_or_changed_astrbot_format_fails_open(self):
        event = Event("测试用户", "123", "456")
        rendered = "[future-format] message"

        self.assertEqual(
            PATCH.rewrite_group_context_record(rendered, event),
            rendered,
        )


class GroupIdentityPatchLifecycleTests(unittest.TestCase):
    MODULE_NAMES = (
        "astrbot",
        "astrbot.builtin_stars",
        "astrbot.builtin_stars.astrbot",
        "astrbot.builtin_stars.astrbot.group_chat_context",
    )

    def setUp(self):
        self.saved_modules = {
            name: sys.modules.get(name)
            for name in self.MODULE_NAMES
        }

        astrbot = types.ModuleType("astrbot")
        builtin_stars = types.ModuleType("astrbot.builtin_stars")
        builtin_astrbot = types.ModuleType("astrbot.builtin_stars.astrbot")
        group_module = types.ModuleType(
            "astrbot.builtin_stars.astrbot.group_chat_context"
        )

        class FakeGroupChatContext:
            async def _format_message(self, event, _cfg):
                return f"[{event.message_obj.sender.nickname}/12:34:56]:  hello"

        group_module.GroupChatContext = FakeGroupChatContext
        astrbot.builtin_stars = builtin_stars
        builtin_stars.astrbot = builtin_astrbot
        builtin_astrbot.group_chat_context = group_module

        sys.modules.update(
            {
                "astrbot": astrbot,
                "astrbot.builtin_stars": builtin_stars,
                "astrbot.builtin_stars.astrbot": builtin_astrbot,
                "astrbot.builtin_stars.astrbot.group_chat_context": group_module,
            }
        )
        self.group_context_cls = FakeGroupChatContext
        self.original_formatter = FakeGroupChatContext._format_message

    def tearDown(self):
        for name, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_install_wraps_and_uninstall_restores_original(self):
        event = Event("测试用户", "2409043649", "1153387215")

        self.assertTrue(PATCH.install_group_identity_context_patch())
        patched = self.group_context_cls._format_message
        self.assertTrue(getattr(patched, "__xiaoman_identity_context_patch__", False))

        rendered = asyncio.run(self.group_context_cls()._format_message(event, {}))
        self.assertEqual(
            rendered,
            "[昵称：测试用户 | QQ号：2409043649 | 群号：1153387215 | 时间：12:34:56]：  hello",
        )

        self.assertTrue(PATCH.uninstall_group_identity_context_patch())
        self.assertIs(
            self.group_context_cls._format_message,
            self.original_formatter,
        )

    def test_reference_count_keeps_patch_during_overlapping_reload(self):
        self.assertTrue(PATCH.install_group_identity_context_patch())
        self.assertTrue(PATCH.install_group_identity_context_patch())

        self.assertTrue(PATCH.uninstall_group_identity_context_patch())
        self.assertTrue(
            getattr(
                self.group_context_cls._format_message,
                "__xiaoman_identity_context_patch__",
                False,
            )
        )

        self.assertTrue(PATCH.uninstall_group_identity_context_patch())
        self.assertIs(
            self.group_context_cls._format_message,
            self.original_formatter,
        )


class AngelHeartIdentityPatchTests(unittest.TestCase):
    MODULE_NAMES = (
        "data.plugins.astrbot_plugin_angel_heart.core.utils.xml_formatter",
        "data.plugins.astrbot_plugin_angel_heart.core.utils.context_utils",
        "data.plugins.astrbot_plugin_angel_heart.core.message_processor",
    )

    def setUp(self):
        self.saved_modules = {
            name: sys.modules.get(name)
            for name in self.MODULE_NAMES
        }

        xml_module = types.ModuleType(self.MODULE_NAMES[0])
        context_module = types.ModuleType(self.MODULE_NAMES[1])
        processor_module = types.ModuleType(self.MODULE_NAMES[2])

        def format_message_to_text(
            msg,
            alias="AngelHeart",
            wrapper_tag=None,
            use_relative_time=False,
        ):
            if (
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and ":GroupMessage:" in str(msg.get("chat_id", ""))
                and msg.get("sender_name") != "tool_result"
                and "sender_name" in msg
            ):
                role_label = msg.get("sender_role") or "群友"
                header = (
                    f"[群友: {msg.get('sender_name')} "
                    f"(ID: {msg.get('sender_id', 'Unknown')}, {role_label})]"
                )
                body = f"{header} (2026-10-05 22:35): {msg.get('content', '')}"
            else:
                body = f"ORIGINAL:{msg.get('content', '')}"

            if wrapper_tag:
                return f"<{wrapper_tag}>\n{body}\n</{wrapper_tag}>"
            return body

        self.original_formatter = format_message_to_text
        xml_module.format_message_to_text = format_message_to_text
        context_module.format_message_to_text = format_message_to_text
        processor_module.format_message_to_text = format_message_to_text

        sys.modules.update(
            {
                self.MODULE_NAMES[0]: xml_module,
                self.MODULE_NAMES[1]: context_module,
                self.MODULE_NAMES[2]: processor_module,
            }
        )
        self.xml_module = xml_module
        self.context_module = context_module
        self.processor_module = processor_module

    def tearDown(self):
        PATCH.uninstall_angelheart_identity_context_patch()
        for name, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_patches_canonical_and_already_imported_formatter_references(self):
        self.assertTrue(PATCH.ensure_angelheart_identity_context_patch())
        self.assertIs(
            self.xml_module.format_message_to_text,
            self.context_module.format_message_to_text,
        )
        self.assertIs(
            self.xml_module.format_message_to_text,
            self.processor_module.format_message_to_text,
        )

        msg = {
            "role": "user",
            "content": "你好",
            "sender_name": "白丝少妇",
            "sender_id": "2409043649",
            "sender_role": "群友",
            "chat_id": "default:GroupMessage:1153387215",
        }
        rendered = self.context_module.format_message_to_text(msg)

        self.assertEqual(
            rendered,
            "[昵称：白丝少妇 | QQ号：2409043649 | 群号：1153387215 | 身份：群友] "
            "(2026-10-05 22:35): 你好",
        )
        self.assertEqual(msg["sender_name"], "白丝少妇")
        self.assertEqual(msg["sender_id"], "2409043649")

    def test_system_notice_like_sender_name_remains_a_labeled_name(self):
        self.assertTrue(PATCH.ensure_angelheart_identity_context_patch())
        msg = {
            "role": "user",
            "content": "欢迎加入",
            "sender_name": "群主邀请了“白丝少妇”加入了群聊",
            "sender_id": "2409043649",
            "sender_role": "群主",
            "chat_id": "default:GroupMessage:1439184369",
        }

        rendered = self.xml_module.format_message_to_text(msg)

        self.assertIn("昵称：群主邀请了“白丝少妇”加入了群聊", rendered)
        self.assertIn("QQ号：2409043649", rendered)
        self.assertIn("群号：1439184369", rendered)
        self.assertIn("身份：群主", rendered)

    def test_private_messages_are_not_rewritten(self):
        self.assertTrue(PATCH.ensure_angelheart_identity_context_patch())
        msg = {
            "role": "user",
            "content": "私聊",
            "sender_name": "用户",
            "sender_id": "10001",
            "chat_id": "default:FriendMessage:10001",
        }

        self.assertEqual(
            self.xml_module.format_message_to_text(msg),
            "ORIGINAL:私聊",
        )

    def test_repeated_ensure_is_idempotent_and_uninstall_restores_all_references(self):
        self.assertTrue(PATCH.ensure_angelheart_identity_context_patch())
        first_wrapper = self.xml_module.format_message_to_text
        self.assertTrue(PATCH.ensure_angelheart_identity_context_patch())
        self.assertIs(self.xml_module.format_message_to_text, first_wrapper)

        self.assertTrue(PATCH.uninstall_angelheart_identity_context_patch())
        self.assertIs(self.xml_module.format_message_to_text, self.original_formatter)
        self.assertIs(self.context_module.format_message_to_text, self.original_formatter)
        self.assertIs(self.processor_module.format_message_to_text, self.original_formatter)


if __name__ == "__main__":
    unittest.main()
