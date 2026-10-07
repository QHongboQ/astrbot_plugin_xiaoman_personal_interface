"""Date-scoped proactive messages derived from existing TimeAwareness snapshots."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .fat_fish_bridge import FatFishBridge
from .time_awareness_adapter import TimeAwarenessAdapter

DEFAULT_PROMPT = (
    "请按每个日程生成一句自然的第一人称聊天消息，只输出 JSON。"
    "忠于事项，不编造地点或同伴，不提日程、系统、AI。"
)


def _zone(name):
    """Resolve IANA zones, with fixed-offset fallbacks for Windows test hosts."""
    try:
        return ZoneInfo(str(name))
    except Exception:
        fixed = {
            "UTC": datetime_timezone.utc,
            "Etc/UTC": datetime_timezone.utc,
            "Asia/Shanghai": datetime_timezone(timedelta(hours=8), "Asia/Shanghai"),
        }
        if str(name) in fixed:
            return fixed[str(name)]
        raise


class ScheduleBroadcastService:
    def __init__(self, context, config, data_dir, *, time_awareness=None, fat_fish=None):
        self.context = context
        self.config = config or {}
        self.cfg = self.config.get("schedule_broadcast", {}) or {}
        self.path = Path(data_dir) / "schedule_broadcast_state.json"
        self.state = {"plans": {}}
        self.day_adapter = time_awareness or TimeAwarenessAdapter(context)
        self.fat_fish = fat_fish or FatFishBridge(context, self.day_adapter)
        self._task = None
        self.last_schedules = {}
        self.last_error = ""
        self._load()

    def _get(self, key, default=None):
        return self.cfg.get(key, default)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("plans"), dict):
                self.state = data
            elif isinstance(data, dict) and isinstance(data.get("entries"), list):
                # Preserve legacy state for inspection; its date cannot be inferred safely.
                self.state = {"plans": {}, "legacy_state": data}
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Schedule broadcast state load failed", exc_info=True)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _now(self, value=None):
        now = value
        if now is None:
            current = getattr(self.day_adapter, "current_time", None)
            now = current() if callable(current) else datetime.now().astimezone()
        if not isinstance(now, datetime):
            now = datetime.now().astimezone()
        return now if now.tzinfo else now.astimezone()

    def _plans(self):
        plans = self.state.get("plans")
        if not isinstance(plans, dict):
            self.state["plans"] = {}
        return self.state["plans"]

    async def targets(self):
        try:
            rows = self.context.conversation_manager.get_conversations()
            rows = await rows
        except Exception:
            return []
        allow = set(self._get("allowlist_umos", []))
        deny = set(self._get("denylist_umos", []))
        result = set()
        for row in rows:
            umo = getattr(row, "user_id", None)
            if not isinstance(umo, str) or umo in deny or (allow and umo not in allow):
                continue
            if ":GroupMessage:" in umo and self._get("send_groups", True):
                result.add(umo)
            elif ":FriendMessage:" in umo and self._get("send_private", True):
                result.add(umo)
        return sorted(result)

    async def read_schedule(self, targets, *, at=None):
        configured = str(self._get("schedule_source_umo", "") or "").strip()
        session = configured or (targets[0] if targets else "")
        if not session:
            return None
        return await self.day_adapter.get_daily_schedule(session, at=at, allow_generate=False)

    async def _provider(self, targets):
        provider = str(self._get("provider_id", "") or "")
        if provider or not targets:
            return provider
        try:
            return await self.context.get_current_chat_provider_id(targets[0])
        except Exception:
            return None

    def _date_at(self, local_now, target_date):
        return local_now.replace(
            year=target_date.year, month=target_date.month, day=target_date.day
        )

    async def _read_date(self, target_date, targets, now):
        schedule = await self.read_schedule(targets, at=self._date_at(now, target_date))
        if schedule is not None:
            self.last_schedules[target_date.isoformat()] = schedule
        else:
            self.last_schedules.pop(target_date.isoformat(), None)
        return schedule

    @staticmethod
    def _clock(value):
        text = str(value or "").strip()
        if text == "24:00":
            return time(0, 0), True
        try:
            return time.fromisoformat(text), False
        except (ValueError, TypeError):
            return None, False

    @staticmethod
    def _parse_absolute(value, timezone):
        try:
            result = datetime.fromisoformat(str(value))
            return result if result.tzinfo else result.replace(tzinfo=_zone(timezone))
        except (TypeError, ValueError, KeyError):
            return None

    @staticmethod
    def _stable_jitter(local_date, period, snapshot_id):
        seed = f"{local_date}|{period}|{snapshot_id}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        return digest[0] % 6, digest[1] % 6

    @staticmethod
    def _is_peak_state(policy):
        return bool(
            policy.get("found")
            and policy.get("enabled")
            and policy.get("provider_affected")
            and policy.get("manual_override", "auto") == "auto"
            and policy.get("state") == "peak"
        )

    def _peak_windows(self, local_date, timezone, day_kind, provider_id):
        if day_kind not in {"workday", "adjusted"}:
            return []
        fish = self.fat_fish.discover()
        if fish is None:
            return []
        try:
            periods = fish._periods()
        except Exception:
            return []
        windows = []
        for index, period in enumerate(periods or [], 1):
            if not isinstance(period, (tuple, list)) or len(period) < 2:
                continue
            start, start_next_day = self._clock(period[0])
            end, end_next_day = self._clock(period[1])
            if start is None or end is None:
                continue
            zone = _zone(timezone)
            peak_start = datetime.combine(local_date, start, tzinfo=zone)
            peak_end = datetime.combine(local_date + timedelta(days=int(end_next_day)), end, tzinfo=zone)
            if not end_next_day and peak_end <= peak_start:
                peak_end += timedelta(days=1)
            midpoint = peak_start + (peak_end - peak_start) / 2
            policy = self.fat_fish.get_wallet_policy(at=midpoint, provider_id=provider_id)
            if not self._is_peak_state(policy):
                continue
            start_jitter, end_jitter = self._stable_jitter(
                local_date.isoformat(), f"{period[0]}-{period[1]}",
                str(getattr(self, "_building_snapshot_id", "")),
            )
            windows.append({
                "index": index,
                "peak_start": peak_start,
                "peak_end": peak_end,
                "cover_start": peak_start - timedelta(minutes=start_jitter),
                "cover_end": peak_end + timedelta(minutes=end_jitter),
                "start_jitter_minutes": start_jitter,
                "end_jitter_minutes": end_jitter,
            })
        return windows

    @staticmethod
    def _overlaps_cover(slot, local_date, timezone, windows):
        start, start_next_day = ScheduleBroadcastService._clock(slot.get("start"))
        end, end_next_day = ScheduleBroadcastService._clock(slot.get("end"))
        if start is None:
            return False
        zone = _zone(timezone)
        event_start = datetime.combine(local_date + timedelta(days=int(start_next_day)), start, tzinfo=zone)
        if end is None:
            event_end = event_start
        else:
            event_end = datetime.combine(local_date + timedelta(days=int(end_next_day)), end, tzinfo=zone)
            if not end_next_day and event_end <= event_start:
                event_end += timedelta(days=1)
        for window in windows:
            left, right = window["cover_start"], window["cover_end"]
            if event_start == event_end:
                if left <= event_start <= right:
                    return True
            elif event_start < right and event_end > left:
                return True
        return False

    def _effective_entries(self, schedule, local_date, timezone, day_kind, provider_id, now):
        self._building_snapshot_id = str(schedule.get("snapshot_id", ""))
        windows = self._peak_windows(local_date, timezone, day_kind, provider_id)
        entries = []
        offset = timedelta(minutes=int(self._get("event_offset_minutes", 0)))
        for index, slot in enumerate(schedule.get("slots", []), 1):
            if not isinstance(slot, dict):
                continue
            if not (str(slot.get("name", "")).strip() or str(slot.get("state", "")).strip()):
                continue
            if self._overlaps_cover(slot, local_date, timezone, windows):
                continue
            start, next_day = self._clock(slot.get("start"))
            if start is None:
                continue
            trigger = datetime.combine(local_date + timedelta(days=int(next_day)), start, tzinfo=_zone(timezone)) + offset
            if trigger + timedelta(seconds=int(self._get("grace_seconds", 60))) < now.astimezone(_zone(timezone)):
                continue
            if any(window["cover_start"] <= trigger <= window["cover_end"] for window in windows):
                continue
            entries.append({
                "id": f"{local_date.isoformat()}-N{index:02d}",
                "kind": "NORMAL",
                "trigger_at": trigger.isoformat(),
                "message": "",
                "sent": False,
                "delivered_umos": [],
                "name": str(slot.get("name", "")),
                "state": str(slot.get("state", "")),
                "slot_start": str(slot.get("start", "")),
                "slot_end": str(slot.get("end", "")),
            })

        raw_context = [
            {key: slot.get(key, "") for key in ("start", "end", "name", "state")}
            for slot in schedule.get("slots", []) if isinstance(slot, dict)
        ]
        for window in windows:
            cover_id = f"{local_date.isoformat()}-P{window['index']:02d}"
            duration = int((window["cover_end"] - window["cover_start"]).total_seconds() // 60)
            context = {
                "activity_id": cover_id,
                "duration_minutes": duration,
                "peak_start": window["peak_start"].isoformat(),
                "peak_end": window["peak_end"].isoformat(),
                "nearby_raw_slots": raw_context,
            }
            for kind, trigger in (("PEAK_START", window["cover_start"]), ("PEAK_END", window["cover_end"])):
                if trigger + timedelta(seconds=int(self._get("grace_seconds", 60))) < now.astimezone(_zone(timezone)):
                    continue
                entries.append({
                    "id": f"{cover_id}-{kind}",
                    "kind": kind,
                    "activity_id": cover_id,
                    "activity_duration_minutes": duration,
                    "trigger_at": trigger.isoformat(),
                    "message": "",
                    "sent": False,
                    "delivered_umos": [],
                    "activity_context": context,
                })
        entries.sort(key=lambda item: item["trigger_at"])
        return entries

    def _prompt_lines(self, entries, schedule, day_kind):
        lines = []
        for entry in entries:
            item = {
                "id": entry["id"],
                "kind": entry["kind"],
                "trigger_at": entry["trigger_at"],
                "name": entry.get("name", ""),
                "state": entry.get("state", ""),
                "activity_context": entry.get("activity_context", {}),
            }
            lines.append(json.dumps(item, ensure_ascii=False))
        instructions = (
            f"目标日期 {schedule.get('local_date', '')}，日期性质 {day_kind}。"
            "为所有 ID 一次性生成消息，输出 JSON 对象且键为 ID。"
            "同一 activity_id 的 PEAK_START 和 PEAK_END 必须描述同一个可信、持续足够久的大型活动；"
            "开始消息表达离开，结束消息表达活动结束/返回。普通 NORMAL 消息忠于原日程。"
        )
        return f"{self._get('broadcast_prompt', DEFAULT_PROMPT)}\n{instructions}\n" + "\n".join(lines)

    def _preserve_delivery(self, old_plan, entries):
        if not isinstance(old_plan, dict):
            return entries
        previous = {
            (entry.get("id"), entry.get("trigger_at")): entry
            for entry in old_plan.get("entries", []) if isinstance(entry, dict)
        }
        for entry in entries:
            old = previous.get((entry["id"], entry["trigger_at"]))
            if old:
                for key in ("message", "sent", "expired", "delivered_umos", "dry_run_logged"):
                    if key in old:
                        entry[key] = old[key]
        return entries

    async def _build_plan(self, schedule, now, provider, targets, *, force=False):
        local_date = date.fromisoformat(str(schedule.get("local_date", "")))
        snapshot_id = str(schedule.get("snapshot_id", ""))
        digest = hashlib.sha256(
            json.dumps(schedule, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        plans = self._plans()
        old_plan = plans.get(local_date.isoformat())
        if (not force and old_plan and old_plan.get("schedule_hash") == digest
                and old_plan.get("plan_complete")):
            return False

        initial_policy = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
        fish_timezone = str(initial_policy.get("timezone") or schedule.get("timezone") or "Asia/Shanghai")
        try:
            _zone(fish_timezone)
        except Exception:
            fish_timezone = str(schedule.get("timezone") or "Asia/Shanghai")
        day_timezone = str(schedule.get("timezone") or fish_timezone)
        day_at = datetime.combine(local_date, time(12, 0), tzinfo=_zone(day_timezone))
        day_policy = self.day_adapter.get_day_policy(day_at)
        day_kind = str(day_policy.get("kind", "unknown")) if day_policy.get("available") else "unknown"

        zone = _zone(fish_timezone)
        effective_entries = self._effective_entries(
            schedule, local_date, fish_timezone, day_kind, provider, now
        )
        effective_entries = self._preserve_delivery(old_plan, effective_entries)
        plan = {
            "local_date": local_date.isoformat(),
            "snapshot_id": snapshot_id,
            "schedule_hash": digest,
            "timezone": fish_timezone,
            "day_kind": day_kind,
            "entries": effective_entries,
            "generated_at": datetime.now(zone).isoformat(),
            "plan_complete": False,
        }
        plans[local_date.isoformat()] = plan

        if not targets:
            self.last_error = "no eligible targets"
            self._save()
            return False
        if not initial_policy.get("found") or not initial_policy.get("allowed"):
            self.last_error = "Fat Fish unavailable or current wallet policy blocked"
            plan["pending"] = True
            self._save()
            return False
        if not effective_entries:
            plan["plan_complete"] = True
            plan.pop("pending", None)
            self._save()
            self.last_error = ""
            return True

        persona = await self.context.persona_manager.get_default_persona_v3()
        system = str(persona.get("prompt", "") or "") if isinstance(persona, dict) else str(getattr(persona, "prompt", "") or "")
        prompt = self._prompt_lines(effective_entries, schedule, day_kind)
        prompt += f"\n每条消息最多{int(self._get('max_message_chars', 80))}字。"
        response = await self.context.llm_generate(
            chat_provider_id=provider, prompt=prompt, system_prompt=system, tools=None
        )
        raw = getattr(response, "completion_text", None) or getattr(response, "text", None) or str(response)
        try:
            output = json.loads(raw)
        except Exception:
            self.last_error = "invalid batch JSON"
            self._save()
            return False
        maximum = int(self._get("max_message_chars", 80))
        for entry in effective_entries:
            text = output.get(entry["id"], "") if isinstance(output, dict) else ""
            if isinstance(text, str):
                entry["message"] = text.replace("\n", " ").strip()[:maximum]
        if any(not entry.get("message") for entry in effective_entries):
            self.last_error = "incomplete batch JSON"
            plan["pending"] = True
            self._save()
            return False
        plan["plan_complete"] = True
        plan.pop("pending", None)
        self._save()
        self.last_error = ""
        return True

    async def refresh(self, force=False, now=None):
        now = self._now(now)
        targets = await self.targets()
        provider = await self._provider(targets)
        dates = (now.date(), now.date() + timedelta(days=1))
        self.last_schedules = {}
        changed = False
        for target_date in dates:
            schedule = await self._read_date(target_date, targets, now)
            if schedule is None:
                continue
            if not provider:
                self.last_error = "provider unavailable"
                continue
            changed = await self._build_plan(schedule, now, provider, targets, force=force) or changed
        if not self.last_schedules:
            self.last_error = "TimeAwareness unavailable or no existing today/tomorrow snapshot"
        return changed

    async def raw_schedule(self, target_date, now=None):
        now = self._now(now)
        targets = await self.targets()
        return await self._read_date(target_date, targets, now)

    async def build_date(self, target_date, now=None):
        now = self._now(now)
        targets = await self.targets()
        schedule = await self._read_date(target_date, targets, now)
        if schedule is None:
            return False, "No existing TimeAwareness snapshot."
        provider = await self._provider(targets)
        if not provider:
            return False, "Provider unavailable."
        result = await self._build_plan(schedule, now, provider, targets, force=True)
        return result, "Plan rebuilt from existing snapshot." if result else self.last_error

    def reset_date(self, target_date):
        removed = self._plans().pop(target_date.isoformat(), None) is not None
        if removed:
            self._save()
        return removed

    def plan_for_date(self, target_date):
        return self._plans().get(target_date.isoformat())

    async def test_entry(self, entry_id, umo):
        if not isinstance(umo, str) or not umo:
            return False, "当前会话不可用。"
        entry = next((entry for plan in self._plans().values()
                      for entry in plan.get("entries", [])
                      if entry.get("id") == entry_id and entry.get("message")), None)
        if entry is None:
            return False, "未找到已生成且可测试的 entry_id。"
        try:
            from astrbot.api.event import MessageChain
            await self.context.send_message(umo, MessageChain().message(entry["message"]))
            return True, f"测试操作：已向当前会话发送 {entry_id}。"
        except Exception:
            logger.warning("Schedule broadcast admin test send failed", exc_info=True)
            return False, "测试操作失败，消息未确认发送。"

    async def send_due(self, now=None):
        targets = await self.targets()
        provider = await self._provider(targets)
        now = self._now(now)
        if targets:
            from astrbot.api.event import MessageChain
        for plan in self._plans().values():
            timezone = str(plan.get("timezone") or "Asia/Shanghai")
            try:
                local_now = now.astimezone(_zone(timezone))
            except Exception:
                local_now = now
            for entry in plan.get("entries", []):
                if entry.get("sent") or entry.get("expired") or not entry.get("message"):
                    continue
                trigger = self._parse_absolute(entry.get("trigger_at"), timezone)
                if trigger is None:
                    entry["expired"] = True
                    self._save()
                    continue
                if local_now > trigger + timedelta(seconds=int(self._get("grace_seconds", 60))):
                    entry["expired"] = True
                    self._save()
                    continue
                if local_now < trigger:
                    continue
                if not targets:
                    continue
                live = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
                if not live.get("found") or not live.get("allowed"):
                    continue
                if self._get("dry_run", False):
                    if not entry.get("dry_run_logged"):
                        logger.info("Schedule broadcast dry-run %s: %s", entry["id"], entry["message"])
                        entry["dry_run_logged"] = True
                        self._save()
                    continue
                delivered = set(entry.get("delivered_umos", []))
                for umo in targets:
                    if umo in delivered:
                        continue
                    live = self.fat_fish.get_wallet_policy(at=now, provider_id=provider)
                    if not live.get("found") or not live.get("allowed"):
                        break
                    try:
                        await self.context.send_message(umo, MessageChain().message(entry["message"]))
                        delivered.add(umo)
                        entry["delivered_umos"] = sorted(delivered)
                        self._save()
                    except Exception:
                        logger.warning("Schedule broadcast send failed", exc_info=True)
                entry["sent"] = bool(targets) and all(umo in delivered for umo in targets)
                self._save()

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
                logger.warning("Schedule broadcast tick failed", exc_info=True)
            await asyncio.sleep(max(1, int(self._get("poll_seconds", 15))))

    def start(self):
        self.fat_fish.install()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="xiaoman-schedule-broadcast")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.fat_fish.uninstall()

    @staticmethod
    def _plan_status(plan):
        if not plan:
            return "missing"
        if plan.get("plan_complete"):
            return "complete"
        return "pending"

    def status(self, now=None):
        now = self._now(now)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        fish = self.fat_fish.get_wallet_policy(provider_id=self._get("provider_id", ""))
        due = [entry for plan in self._plans().values() for entry in plan.get("entries", [])
               if entry.get("message") and not entry.get("sent") and not entry.get("expired")]
        due.sort(key=lambda entry: entry.get("trigger_at", ""))
        return {
            "enabled": bool(self._get("enable", False)),
            "dry_run": bool(self._get("dry_run", False)),
            "time_awareness_found": self.day_adapter.discover() is not None,
            "fat_fish_found": fish.get("found", False),
            "day_kind": fish.get("day_kind", "unknown"),
            "today_snapshot": self.last_schedules.get(today.isoformat()),
            "tomorrow_snapshot": self.last_schedules.get(tomorrow.isoformat()),
            "today_plan_status": self._plan_status(self.plan_for_date(today)),
            "tomorrow_plan_status": self._plan_status(self.plan_for_date(tomorrow)),
            "next": due[0] if due else None,
            "last_error": self.last_error,
        }
