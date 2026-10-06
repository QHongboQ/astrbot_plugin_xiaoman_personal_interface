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


if __name__ == "__main__":
    unittest.main()
