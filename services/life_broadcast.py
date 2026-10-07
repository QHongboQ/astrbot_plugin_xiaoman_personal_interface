"""Optional, read-only adapter from Life Scheduler to existing conversations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger


LIFE_SCHEDULER_NAME = "astrbot_plugin_life_scheduler"
FAT_FISH_NAME = "astrbot_plugin_fat_fish_wallet"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PROMPT = "请按每个日程生成一句自然的第一人称 QQ 聊天消息，只输出 JSON。忠于事项，不编造地点或同伴，不提日程、系统、AI，不用表情、Markdown、括号或省略号。"


def parse_schedule(text: str) -> list[dict[str, str]]:
    """Parse valid schedule rows; tolerate ASCII/fullwidth separators."""
    rows = []
    for line in (text or "").splitlines():
        fields = [part.strip() for part in re.split(r"[|｜]", line)]
        if len(fields) < 3:
            continue
        time_match = re.fullmatch(
            r"(?P<start>(?:[01]?\d|2[0-3]):[0-5]\d)(?:\s*[-–]\s*(?:[01]?\d|2[0-3]):[0-5]\d)?",
            fields[0],
        )
        data: dict[str, str] = {
            "time": time_match.group("start") if time_match else "",
            "location": "",
            "activity": "",
            "detail": "",
            "raw": line.strip(),
        }
        if len(fields) >= 4:
            for field in fields[1:]:
                match = re.match(r"^(时间|地点|事项|细节)\s*[:：]\s*(.*)$", field)
                if match:
                    data[{"时间": "time", "地点": "location", "事项": "activity", "细节": "detail"}[match.group(1)]] = match.group(2).strip()
            if not data["location"]:
                bare = [f for f in fields[1:] if not re.match(r"^(时间|地点|事项|细节)\s*[:：]", f)]
                if len(bare) >= 3:
                    data["location"], data["activity"], data["detail"] = bare[-3:]
        elif len(fields) == 3:
            legacy = re.match(r"^(.+?)\s*[:：]\s*(.+)$", fields[1])
            if legacy:
                data["location"] = legacy.group(1).strip()
                data["activity"] = legacy.group(2).strip()
                data["detail"] = fields[2]
        if not data["time"]:
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
        self.last_provider_id = None
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

    @staticmethod
    def _cfg_value(config, key, default=None):
        try:
            return config.get(key, default)
        except Exception:
            return default

    def fat_fish_config(self):
        """Read only the active Fat Fish plugin's public StarMetadata.config."""
        try:
            for metadata in self.context.get_all_stars():
                if getattr(metadata, "name", None) == FAT_FISH_NAME:
                    if getattr(metadata, "activated", False) is not True:
                        return None
                    return getattr(metadata, "config", None)
        except Exception:
            logger.warning("Fat Fish discovery failed", exc_info=True)
        return None

    @staticmethod
    def _timezone(config):
        name = str(LifeBroadcastService._cfg_value(config, "timezone", "") or DEFAULT_TIMEZONE)
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Invalid Fat Fish timezone %r; using %s", name, DEFAULT_TIMEZONE)
            return ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE

    @staticmethod
    def _local_now(zone, now=None):
        now = now or datetime.now(zone)
        return now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)

    @staticmethod
    def _peak_period_ranges(config):
        periods = LifeBroadcastService._cfg_value(config, "peak_periods", "") or ""
        if isinstance(periods, str):
            return [part.strip() for part in periods.split(",") if part.strip()]
        if isinstance(periods, (list, tuple)):
            return [str(part).strip() for part in periods if str(part).strip()]
        return []

    @staticmethod
    def _peak_at(config, local_dt):
        weekdays = LifeBroadcastService._cfg_value(config, "peak_weekdays", []) or []
        if isinstance(weekdays, str):
            weekdays = [part.strip() for part in weekdays.split(",") if part.strip()]
        try:
            weekdays = {int(day) for day in weekdays}
        except (TypeError, ValueError):
            weekdays = set()
        if weekdays and local_dt.weekday() not in weekdays:
            return False
        for period in LifeBroadcastService._peak_period_ranges(config):
            try:
                start_s, end_s = period.split("-", 1)
                start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
                current = local_dt.time()
                in_period = start <= current < end if start <= end else current >= start or current < end
                if in_period:
                    return True
            except (ValueError, TypeError):
                logger.warning("Invalid Fat Fish peak period: %r", period)
        return False

    @staticmethod
    def _provider_affected(config, provider_id):
        affected = LifeBroadcastService._cfg_value(config, "affected_providers", []) or []
        if isinstance(affected, str):
            affected = [part.strip() for part in affected.split(",") if part.strip()]
        affected = [str(item).strip() for item in affected if str(item).strip()]
        if not affected:
            return False
        if "*" in affected:
            return True
        if not provider_id:
            return bool(LifeBroadcastService._cfg_value(config, "gate_when_provider_unknown", True))
        provider = str(provider_id).casefold()
        return any(keyword.casefold() in provider for keyword in affected)

    def fat_fish_policy(self, now=None, provider_id=None):
        config = self.fat_fish_config()
        if config is None:
            return {"found": False, "enabled": False, "timezone": DEFAULT_TIMEZONE, "state": "missing", "allowed": False, "provider_affected": False, "manual_override": "auto", "periods": [], "weekdays": []}
        zone, timezone_name = self._timezone(config)
        local_now = self._local_now(zone, now)
        enabled = bool(self._cfg_value(config, "enabled", False))
        override = str(self._cfg_value(config, "manual_override", "auto") or "auto").casefold()
        affected = self._provider_affected(config, provider_id)
        periods = self._peak_period_ranges(config)
        weekdays = self._cfg_value(config, "peak_weekdays", []) or []
        if isinstance(weekdays, str):
            weekdays = [p.strip() for p in weekdays.split(",") if p.strip()]
        state, allowed = "offpeak", True
        holiday_today = False
        try:
            import holidays
            holiday_today = local_now.date() in holidays.CN()
        except Exception:
            logger.warning("Chinese holiday calendar unavailable; using Fat Fish weekday/time policy", exc_info=True)
        if enabled and affected:
            if override == "always_allow":
                state, allowed = "forced allow", True
            elif override == "always_block":
                state, allowed = "forced block", False
            else:
                peak = False if holiday_today else self._peak_at(config, local_now)
                state, allowed = ("peak", False) if peak else ("offpeak", True)
        return {"found": True, "enabled": enabled, "timezone": timezone_name, "manual_override": override, "periods": periods, "weekdays": weekdays, "state": state, "allowed": allowed, "provider_affected": affected, "holiday_today": holiday_today, "local_now": local_now}

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

    async def _provider_for_targets(self, targets):
        provider = self._get("provider_id", "")
        if provider:
            self.last_provider_id = provider
            return provider
        if targets:
            try:
                provider = await self.context.get_current_chat_provider_id(targets[0])
                self.last_provider_id = provider
                return provider
            except Exception:
                return None
        self.last_provider_id = None
        return None

    async def _generate(self, rows, targets, provider, now: datetime | None = None):
        self.last_error = ""
        if not rows:
            return []
        if not targets:
            return None
        offset = int(self._get("event_offset_minutes", 0))
        fish_config = self.fat_fish_config()
        if fish_config is None:
            self.last_error = "Fat Fish unavailable"
            return None
        zone, _ = self._timezone(fish_config)
        generation_now = self._local_now(zone, now)
        eligible = []
        for index, row in enumerate(rows, 1):
            trigger_dt = self._trigger_datetime(row["time"], offset, generation_now)
            if not self.fat_fish_policy(trigger_dt, provider).get("allowed", False) or self._expired_at(trigger_dt, generation_now):
                continue
            entry_id = f"E{index}"
            eligible.append((entry_id, row, trigger_dt))
        if not eligible:
            return []
        if not provider:
            self.last_error = "provider unavailable"
            logger.warning("Xiaoman broadcast pending: no chat provider")
            return None
        persona = await self.context.persona_manager.get_default_persona_v3()
        if isinstance(persona, dict):
            persona_prompt = str(persona.get("prompt", "") or "")
        else:
            persona_prompt = str(getattr(persona, "prompt", "") or "")
        inputs = []
        entries = []
        for entry_id, row, trigger_dt in eligible:
            inputs.append(f"{entry_id} | {row['time']} | {row['location']} | {row['activity']} | {row['detail']}")
            entries.append({"id": entry_id, "time": row["time"], "trigger_time": trigger_dt.strftime("%H:%M"), "location": row["location"], "activity": row["activity"], "detail": row["detail"], "message": "", "sent": False, "delivered_umos": []})
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
            if not self.fat_fish_policy(provider_id=provider).get("allowed", False):
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

    @staticmethod
    def _trigger_datetime(hhmm: str, offset: int, now: datetime) -> datetime:
        event_time = time.fromisoformat(hhmm)
        return datetime.combine(now.date(), event_time, tzinfo=now.tzinfo) + timedelta(minutes=offset)

    def _expired_at(self, trigger: datetime, now: datetime) -> bool:
        return now > trigger + timedelta(seconds=int(self._get("grace_seconds", 60)))

    async def refresh(self, force=False, now: datetime | None = None):
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
        fish_config = self.fat_fish_config()
        if fish_config is None:
            self.state.update(pending_hash=digest, pending_text=text)
            self._save()
            return False
        zone, _ = self._timezone(fish_config)
        refresh_now = self._local_now(zone, now)
        targets = await self.targets()
        self.last_target_count = len(targets)
        if not targets:
            self.state.update(pending_hash=digest, pending_text=text)
            return False
        provider = await self._provider_for_targets(targets)
        current_policy = self.fat_fish_policy(refresh_now, provider)
        if not current_policy.get("found") or not current_policy.get("allowed"):
            self.state["pending_hash"] = digest
            self.state["pending_text"] = text
            self._save()
            return False
        rows = parse_schedule(text)
        try:
            entries = await self._generate(rows, targets, provider, now=refresh_now)
        except Exception as exc:
            self.last_error = f"generation failed: {exc}"
            entries = None
        if entries is None:
            if self.last_error not in {"provider unavailable", "blocked during JSON repair", "Fat Fish unavailable"}:
                self.state.update(failed_hash=digest, failure_reason=self.last_error)
            self.state.update(pending_hash=digest, pending_text=text)
            self._save()
            return False
        self.state = {"schedule_hash": digest, "schedule_text": text, "generated_at": datetime.now().isoformat(), "entries": entries, "plan_complete": True}
        self._save()
        return True

    def _expired(self, entry, now):
        try:
            fish_config = self.fat_fish_config()
            zone, _ = self._timezone(fish_config) if fish_config is not None else (ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE)
            local_now = self._local_now(zone, now)
            event = datetime.combine(local_now.date(), time.fromisoformat(entry["trigger_time"]), tzinfo=zone)
            return local_now > event + timedelta(seconds=int(self._get("grace_seconds", 60)))
        except (ValueError, KeyError):
            return True

    async def send_due(self, now=None):
        fish_config = self.fat_fish_config()
        if fish_config is None:
            return
        zone, _ = self._timezone(fish_config)
        now = self._local_now(zone, now)
        targets = await self.targets()
        self.last_target_count = len(targets)
        provider = await self._provider_for_targets(targets)
        for entry in self.state.get("entries", []):
            if entry.get("sent") or entry.get("expired") or not entry.get("message"):
                continue
            try:
                trigger = datetime.combine(now.date(), time.fromisoformat(entry["trigger_time"]), tzinfo=now.tzinfo)
            except (ValueError, KeyError):
                entry["expired"] = True
                self._save()
                continue
            if self._expired_at(trigger, now):
                entry["expired"] = True
                self._save()
                continue
            if now < trigger:
                continue
            policy = self.fat_fish_policy(now, provider)
            if not policy.get("found") or not policy.get("allowed"):
                continue
            if self._get("dry_run", False):
                if not entry.get("dry_run_logged"):
                    logger.info("Xiaoman broadcast dry-run due: %s", entry["message"])
                    entry["dry_run_logged"] = True
                    self._save()
                continue
            from astrbot.api.event import MessageChain
            delivered = set(entry.get("delivered_umos", []))
            entry["delivered_umos"] = sorted(delivered)
            if not targets:
                continue
            failures = 0
            for umo in targets:
                if umo in delivered:
                    continue
                policy = self.fat_fish_policy(now, provider)
                if not policy.get("found") or not policy.get("allowed"):
                    break
                try:
                    await self.context.send_message(umo, MessageChain().message(entry["message"]))
                    delivered.add(umo)
                    entry["delivered_umos"] = sorted(delivered)
                    self._save()
                except Exception:
                    failures += 1
                    logger.warning("Xiaoman broadcast send failed for %s", umo, exc_info=True)
            entry["sent"] = bool(targets) and all(umo in delivered for umo in targets)
            self._save()
            logger.info("Xiaoman broadcast delivery: delivered=%s failure=%s", len(delivered), failures)

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
        provider = self._get("provider_id", "") or getattr(self, "last_provider_id", None)
        policy = self.fat_fish_policy(provider_id=provider)
        return {"enabled": bool(self._get("enable", False)), "dry_run": bool(self._get("dry_run", False)), "life_scheduler_found": self.last_found, "hash": self.state.get("schedule_hash", "")[:12], "node_count": len(parse_schedule(self.state.get("schedule_text", ""))), "eligible_count": len(entries), "sent_count": sum(bool(e.get("sent")) for e in entries), "target_count": self.last_target_count, "next": pending[0] if pending else None, "fat_fish": policy}
