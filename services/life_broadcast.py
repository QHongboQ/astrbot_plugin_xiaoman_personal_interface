"""Optional, read-only adapter from Life Scheduler to existing conversations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path

from astrbot.api import logger


LIFE_SCHEDULER_NAME = "astrbot_plugin_life_scheduler"
DEFAULT_WINDOWS = ("09:00-12:00", "14:00-18:00")
DEFAULT_PROMPT = "请按每个日程生成一句自然的第一人称 QQ 聊天消息，只输出 JSON。忠于事项，不编造地点或同伴，不提日程、系统、AI，不用表情、Markdown、括号或省略号。"


def parse_schedule(text: str) -> list[dict[str, str]]:
    """Parse valid schedule rows; tolerate ASCII/fullwidth separators."""
    rows = []
    for line in (text or "").splitlines():
        fields = [part.strip() for part in re.split(r"[|｜]", line)]
        if len(fields) < 4:
            continue
        data: dict[str, str] = {"time": "", "location": "", "activity": "", "detail": "", "raw": line.strip()}
        for field in fields:
            match = re.match(r"^(时间|地点|事项|细节)\s*[:：]\s*(.*)$", field)
            if match:
                data[{"时间": "time", "地点": "location", "事项": "activity", "细节": "detail"}[match.group(1)]] = match.group(2).strip()
            elif re.fullmatch(r"\d{1,2}:\d{2}", field):
                data["time"] = field
        # Canonical contract also permits an unlabelled four-field pipe row.
        bare = [f for f in fields if not re.match(r"^(时间|地点|事项|细节)\s*[:：]", f) and not re.fullmatch(r"\d{1,2}:\d{2}", f)]
        if not data["location"] and len(bare) >= 3:
            data["location"], data["activity"], data["detail"] = bare[-3:]
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", data["time"]):
            logger.debug("Skipping malformed Life Scheduler row: %s", line)
            continue
        if not (data["activity"] or data["detail"]):
            logger.debug("Skipping incomplete Life Scheduler row: %s", line)
            continue
        data["time"] = f"{int(data['time'][:data['time'].index(':')]):02d}:{data['time'].split(':')[1]}"
        rows.append(data)
    return rows


class LifeBroadcastService:
    def __init__(self, context, config: dict, data_dir: str | Path):
        self.context = context
        self.config = config or {}
        self.cfg = self.config.get("life_broadcast", {})
        self.path = Path(data_dir) / "life_broadcast_state.json"
        self.state: dict = {"schedule_hash": "", "schedule_text": "", "generated_at": "", "entries": []}
        self._task: asyncio.Task | None = None
        self.last_found = False
        self.last_target_count = 0
        self.last_error = ""
        self._load()

    def _get(self, key, default):
        return self.cfg.get(key, default)

    def _load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                self.state = loaded
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Xiaoman broadcast state unreadable; starting empty", exc_info=True)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def blocked(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        current = now.time()
        for window in self._get("blocked_windows", list(DEFAULT_WINDOWS)):
            try:
                start_s, end_s = window.split("-", 1)
                start = time.fromisoformat(start_s.strip())
                end = time.fromisoformat(end_s.strip())
                if start <= end and start <= current < end or start > end and (current >= start or current < end):
                    return True
            except (ValueError, TypeError):
                logger.warning("Invalid broadcast blocked window: %r", window)
        return False

    def discover(self):
        try:
            for metadata in self.context.get_all_stars():
                if getattr(metadata, "name", None) == LIFE_SCHEDULER_NAME and getattr(metadata, "activated", False):
                    instance = getattr(metadata, "star_cls", None)
                    if instance is not None:
                        return instance
        except Exception:
            logger.warning("Life Scheduler discovery failed", exc_info=True)
        return None

    async def read_schedule(self):
        plugin = self.discover()
        self.last_found = plugin is not None
        if plugin is None:
            return None
        try:
            result = await plugin.get_life_context(allow_generate=False)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                for key in ("schedule_text", "schedule", "life_context", "context"):
                    if isinstance(result.get(key), str):
                        return result[key]
            return str(result) if result else ""
        except Exception:
            logger.warning("Life Scheduler read failed", exc_info=True)
            return None

    async def targets(self):
        try:
            conversations = self.context.conversation_manager.get_conversations()
            if inspect.isawaitable(conversations):
                conversations = await conversations
        except Exception:
            logger.warning("Could not enumerate AstrBot conversations", exc_info=True)
            return []
        allow = set(self._get("allowlist_umos", []))
        deny = set(self._get("denylist_umos", []))
        mode = self._get("target_mode", "all_conversations")
        found = set()
        for conversation in conversations:
            umo = getattr(conversation, "user_id", None)
            if not isinstance(umo, str) or umo in deny or (mode == "allowlist" and umo not in allow):
                continue
            if ":GroupMessage:" in umo and self._get("send_groups", True):
                found.add(umo)
            elif ":FriendMessage:" in umo and self._get("send_private", True):
                found.add(umo)
        return sorted(found)

    def _event_time(self, hhmm: str, offset: int) -> str:
        h, m = map(int, hhmm.split(":"))
        shifted = (datetime(2000, 1, 1, h, m) + timedelta(minutes=offset)).time()
        return shifted.strftime("%H:%M")

    async def _generate(self, rows, targets):
        self.last_error = ""
        if not rows or not targets:
            return None
        provider = self._get("provider_id", "")
        if not provider:
            try:
                provider = await self.context.get_current_chat_provider_id(targets[0])
            except Exception:
                provider = None
        if not provider:
            self.last_error = "provider unavailable"
            logger.warning("Xiaoman broadcast pending: no chat provider")
            return None
        persona = await self.context.persona_manager.get_default_persona_v3()
        persona_prompt = getattr(persona, "prompt", "") or ""
        offset = int(self._get("event_offset_minutes", 0))
        inputs = []
        entries = []
        for index, row in enumerate(rows, 1):
            trigger = self._event_time(row["time"], offset)
            if self._time_blocked(trigger):
                continue
            entry_id = f"E{index}"
            inputs.append(f"{entry_id} | {row['time']} | {row['location']} | {row['activity']} | {row['detail']}")
            entries.append({"id": entry_id, "time": row["time"], "trigger_time": trigger, "location": row["location"], "activity": row["activity"], "detail": row["detail"], "message": "", "sent": False})
        if not entries:
            return []
        user_prompt = f"{self._get('broadcast_prompt', DEFAULT_PROMPT)}\n事件 offset={offset} 分钟。每项生成 15-60 字消息（最长 {self._get('max_message_chars', 80)} 字），只输出 JSON 对象，键为事件 ID。\n" + "\n".join(inputs)
        response = await self.context.llm_generate(chat_provider_id=provider, prompt=user_prompt, system_prompt=persona_prompt, tools=None)
        raw = getattr(response, "completion_text", None) or getattr(response, "text", None) or str(response)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
        except Exception:
            # One bounded repair request, only while outside blocked windows.
            if self.blocked():
                self.last_error = "blocked during JSON repair"
                return None
            repaired = await self.context.llm_generate(chat_provider_id=provider, prompt="请只修复为合法 JSON 对象，保留事件 ID 与消息内容：\n" + raw, system_prompt=persona_prompt, tools=None)
            raw2 = getattr(repaired, "completion_text", None) or getattr(repaired, "text", None) or str(repaired)
            try:
                payload = json.loads(raw2)
                if not isinstance(payload, dict):
                    return None
            except Exception:
                self.last_error = "invalid JSON after one repair"
                return None
        maximum = int(self._get("max_message_chars", 80))
        for entry in entries:
            value = payload.get(entry["id"])
            if isinstance(value, str):
                value = value.strip().strip('"“”\'')
                entry["message"] = value[:maximum].replace("\r", " ").replace("\n", " ").strip()
        return entries

    def _time_blocked(self, hhmm):
        try:
            current = datetime.combine(datetime.now().date(), time.fromisoformat(hhmm))
            return self.blocked(current)
        except ValueError:
            return True

    async def refresh(self, force=False):
        text = await self.read_schedule()
        if text is None:
            return False
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == self.state.get("failed_hash") and not force:
            return False
        if digest == self.state.get("schedule_hash"):
            # Same hash reuses the durable plan. Only incomplete/missing plans retry.
            incomplete = not self.state.get("plan_complete", False)
            pending = self.state.get("pending_hash") == digest
            if not incomplete and not pending:
                return False
        if self.blocked():
            self.state["pending_hash"] = digest
            self.state["pending_text"] = text
            return False
        targets = await self.targets()
        self.last_target_count = len(targets)
        if not targets:
            self.state.update(pending_hash=digest, pending_text=text)
            return False
        rows = parse_schedule(text)
        try:
            entries = await self._generate(rows, targets)
        except Exception as exc:
            self.last_error = f"generation failed: {exc}"
            entries = None
        if entries is None:
            if self.last_error not in {"provider unavailable", "blocked during JSON repair"}:
                self.state.update(failed_hash=digest, failure_reason=self.last_error)
            self.state.update(pending_hash=digest, pending_text=text)
            self._save()
            return False
        self.state = {"schedule_hash": digest, "schedule_text": text, "generated_at": datetime.now().isoformat(), "entries": entries, "plan_complete": True}
        self._save()
        return True

    def _expired(self, entry, now):
        try:
            event = datetime.combine(now.date(), time.fromisoformat(entry["trigger_time"]))
            return now > event + timedelta(seconds=int(self._get("grace_seconds", 60)))
        except (ValueError, KeyError):
            return True

    async def send_due(self, now=None):
        now = now or datetime.now()
        if self.blocked(now):
            return
        targets = await self.targets()
        self.last_target_count = len(targets)
        for entry in self.state.get("entries", []):
            if entry.get("sent") or entry.get("expired") or not entry.get("message"):
                continue
            try:
                trigger = datetime.combine(now.date(), time.fromisoformat(entry["trigger_time"]))
            except (ValueError, KeyError):
                entry["expired"] = True
                self._save()
                continue
            if now > trigger + timedelta(seconds=int(self._get("grace_seconds", 60))):
                entry["expired"] = True
                self._save()
                continue
            if now < trigger:
                continue
            if self._get("dry_run", False):
                if not entry.get("dry_run_logged"):
                    logger.info("Xiaoman broadcast dry-run due: %s", entry["message"])
                    entry["dry_run_logged"] = True
                    self._save()
                continue
            from astrbot.api.event import MessageChain
            successes = failures = 0
            for umo in targets:
                try:
                    await self.context.send_message(umo, MessageChain().message(entry["message"]))
                    successes += 1
                except Exception:
                    failures += 1
                    logger.warning("Xiaoman broadcast send failed for %s", umo, exc_info=True)
            entry["sent"] = True
            entry["sent_count"] = successes
            self._save()
            logger.info("Xiaoman broadcast sent: success=%s failure=%s", successes, failures)

    async def tick(self):
        await self.refresh()
        await self.send_due()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Xiaoman broadcast tick failed", exc_info=True)
            await asyncio.sleep(max(1, int(self._get("poll_seconds", 15))))

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="xiaoman-life-broadcast")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self):
        entries = self.state.get("entries", [])
        pending = [e for e in entries if not e.get("sent") and not e.get("expired") and e.get("message")]
        return {"enabled": bool(self._get("enable", False)), "dry_run": bool(self._get("dry_run", False)), "life_scheduler_found": self.last_found, "hash": self.state.get("schedule_hash", "")[:12], "node_count": len(parse_schedule(self.state.get("schedule_text", ""))), "eligible_count": len(entries), "sent_count": sum(bool(e.get("sent")) for e in entries), "target_count": self.last_target_count, "next": pending[0] if pending else None, "blocked_windows": self._get("blocked_windows", list(DEFAULT_WINDOWS))}
