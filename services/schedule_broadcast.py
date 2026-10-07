"""Persona-driven proactive messages from TimeAwareness effective schedules."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import datetime, time, timedelta
from pathlib import Path

from astrbot.api import logger
from .time_awareness_adapter import TimeAwarenessAdapter
from .fat_fish_bridge import FatFishBridge

DEFAULT_PROMPT = "请按每个日程生成一句自然的第一人称聊天消息，只输出 JSON。忠于事项，不编造地点或同伴，不提日程、系统、AI。"


class ScheduleBroadcastService:
    def __init__(self, context, config, data_dir, *, time_awareness=None, fat_fish=None):
        self.context = context
        self.config = config or {}
        self.cfg = self.config.get("schedule_broadcast", self.config.get("life_broadcast", {})) or {}
        self.path = Path(data_dir) / "schedule_broadcast_state.json"
        self.state = {"schedule_hash": "", "entries": [], "generated_at": ""}
        self.day_adapter = time_awareness or TimeAwarenessAdapter(context)
        self.fat_fish = fat_fish or FatFishBridge(context, self.day_adapter)
        self._task = None
        self.last_schedule = None
        self.last_error = ""
        self.last_targets = []
        self.last_source = ""
        self._load()

    def _get(self, key, default=None): return self.cfg.get(key, default)
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list): self.state = data
        except FileNotFoundError: pass
        except Exception: logger.warning("Schedule broadcast state load failed", exc_info=True)
    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def targets(self):
        try:
            rows = self.context.conversation_manager.get_conversations()
            if inspect.isawaitable(rows): rows = await rows
        except Exception: return []
        allow, deny = set(self._get("allowlist_umos", [])), set(self._get("denylist_umos", []))
        result = set()
        for row in rows:
            umo = getattr(row, "user_id", None)
            if not isinstance(umo, str) or umo in deny or (allow and umo not in allow): continue
            if ":GroupMessage:" in umo and self._get("send_groups", True): result.add(umo)
            elif ":FriendMessage:" in umo and self._get("send_private", True): result.add(umo)
        return sorted(result)

    async def read_schedule(self, targets, *, at=None):
        configured = str(self._get("schedule_source_umo", "") or "").strip()
        session = configured or (targets[0] if targets else "")
        self.last_source = session
        if not session: return None
        return await self.day_adapter.get_daily_schedule(session, at=at, allow_generate=False)

    async def _provider(self, targets):
        provider = str(self._get("provider_id", "") or "")
        if provider or not targets: return provider
        try: return await self.context.get_current_chat_provider_id(targets[0])
        except Exception: return None

    @staticmethod
    def _parse_time(value):
        try: return time.fromisoformat(str(value))
        except (ValueError, TypeError): return None

    def _trigger(self, slot, local_now):
        start = self._parse_time(slot.get("start"))
        if start is None: return None
        return datetime.combine(local_now.date(), start, tzinfo=local_now.tzinfo) + timedelta(minutes=int(self._get("event_offset_minutes", 0)))

    async def refresh(self, force=False, now=None):
        targets = await self.targets(); self.last_targets = targets
        schedule = await self.read_schedule(targets, at=now)
        self.last_schedule = schedule
        if schedule is None:
            self.last_error = "TimeAwareness unavailable or schedule snapshot missing"
            return False
        payload = json.dumps(schedule, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if digest == self.state.get("schedule_hash") and self.state.get("plan_complete"): return False
        if not targets:
            self.last_error = "no eligible targets"; return False
        provider = await self._provider(targets)
        if not provider:
            self.last_error = "provider unavailable"; return False
        policy = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
        if not policy.get("found") or not policy.get("allowed"):
            self.last_error = "Fat Fish unavailable or blocked"; self.state["pending_hash"] = digest; self._save(); return False
        local_now = policy.get("evaluated_at") or now
        if not isinstance(local_now, datetime): local_now = datetime.now().astimezone()
        eligible = []
        for i, slot in enumerate(schedule.get("slots", []), 1):
            if not isinstance(slot, dict) or not (str(slot.get("name", "")).strip() or str(slot.get("state", "")).strip()): continue
            trigger = self._trigger(slot, local_now)
            if trigger is None or trigger + timedelta(seconds=int(self._get("grace_seconds", 60))) < local_now: continue
            future = self.fat_fish.get_wallet_policy(at=trigger, provider_id=provider)
            if future.get("found") and future.get("allowed"):
                eligible.append((f"E{i}", slot, trigger))
        if not eligible:
            self.state = {"schedule_hash": digest, "entries": [], "generated_at": datetime.now().isoformat(), "plan_complete": True}; self._save(); return True
        persona = await self.context.persona_manager.get_default_persona_v3()
        system = str(persona.get("prompt", "") or "") if isinstance(persona, dict) else str(getattr(persona, "prompt", "") or "")
        lines, entries = [], []
        for eid, slot, trigger in eligible:
            lines.append(json.dumps({"id": eid, **{k: slot.get(k, "") for k in ("start", "end", "name", "state", "origin")}}, ensure_ascii=False))
            entries.append({"id": eid, "trigger_time": trigger.strftime("%H:%M"), "message": "", "sent": False, "delivered_umos": []})
        prompt = f"{self._get('broadcast_prompt', DEFAULT_PROMPT)}\n每项最多{int(self._get('max_message_chars', 80))}字，只输出JSON对象，键为事件ID。\n" + "\n".join(lines)
        response = await self.context.llm_generate(chat_provider_id=provider, prompt=prompt, system_prompt=system, tools=None)
        raw = getattr(response, "completion_text", None) or getattr(response, "text", None) or str(response)
        try: output = json.loads(raw)
        except Exception:
            self.last_error = "invalid batch JSON"; return False
        maximum = int(self._get("max_message_chars", 80))
        for entry in entries:
            text = output.get(entry["id"], "") if isinstance(output, dict) else ""
            if isinstance(text, str): entry["message"] = text.replace("\n", " ").strip()[:maximum]
        self.state = {"schedule_hash": digest, "entries": entries, "generated_at": datetime.now().isoformat(), "plan_complete": True, "snapshot_id": schedule.get("snapshot_id", "")}
        self._save(); self.last_error = ""; return True

    async def send_due(self, now=None):
        targets = await self.targets(); self.last_targets = targets
        provider = await self._provider(targets)
        policy = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
        local_now = policy.get("evaluated_at") or now
        if not isinstance(local_now, datetime): local_now = datetime.now().astimezone()
        from astrbot.api.event import MessageChain
        for entry in self.state.get("entries", []):
            if entry.get("sent") or entry.get("expired") or not entry.get("message"): continue
            try: trigger = datetime.combine(local_now.date(), time.fromisoformat(entry["trigger_time"]), tzinfo=local_now.tzinfo)
            except Exception: entry["expired"] = True; self._save(); continue
            if local_now > trigger + timedelta(seconds=int(self._get("grace_seconds", 60))): entry["expired"] = True; self._save(); continue
            if local_now < trigger: continue
            live = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
            if not live.get("found") or not live.get("allowed"): continue
            if self._get("dry_run", False): entry["dry_run_logged"] = True; self._save(); continue
            delivered = set(entry.get("delivered_umos", []))
            for umo in targets:
                if umo in delivered: continue
                live = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
                if not live.get("found") or not live.get("allowed"): break
                try:
                    await self.context.send_message(umo, MessageChain().message(entry["message"]))
                    delivered.add(umo); entry["delivered_umos"] = sorted(delivered); self._save()
                except Exception: logger.warning("Schedule broadcast send failed", exc_info=True)
            entry["sent"] = bool(targets) and all(umo in delivered for umo in targets); self._save()

    async def tick(self): await self.refresh(); await self.send_due()
    async def run(self):
        while True:
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception: logger.warning("Schedule broadcast tick failed", exc_info=True)
            await asyncio.sleep(max(1, int(self._get("poll_seconds", 15))))
    def start(self):
        self.fat_fish.install()
        if self._task is None or self._task.done(): self._task = asyncio.create_task(self.run(), name="xiaoman-schedule-broadcast")
    async def stop(self):
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None
        self.fat_fish.uninstall()
    def status(self):
        schedule = self.last_schedule or {}
        fish = self.fat_fish.get_wallet_policy(provider_id=self._get("provider_id", ""))
        pending = next((e for e in self.state.get("entries", []) if not e.get("sent") and not e.get("expired")), None)
        return {"enabled": bool(self._get("enable", False)), "dry_run": bool(self._get("dry_run", False)),
                "time_awareness_found": self.day_adapter.discover() is not None, "schedule_source": self.last_source,
                "time_awareness_version": self.day_adapter.version(), "fat_fish_version": self.fat_fish.version(),
                "snapshot_id": schedule.get("snapshot_id", self.state.get("snapshot_id", "")), "local_date": schedule.get("local_date", ""),
                "slot_count": len(schedule.get("slots", [])), "generated": bool(self.state.get("generated_at")),
                "sent": sum(bool(e.get("sent")) for e in self.state.get("entries", [])), "targets": len(self.last_targets),
                "next": pending, "last_error": self.last_error, "fat_fish": fish}
