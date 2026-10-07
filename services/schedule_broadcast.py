"""Date-scoped proactive messages derived from existing TimeAwareness snapshots."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
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
        candidates = []
        for row in rows:
            umo = getattr(row, "user_id", None)
            parsed = self._parse_umo(umo)
            if parsed is None:
                continue
            platform_name, message_type, session_id = parsed
            if message_type == "GroupMessage" and not self._get("send_groups", True):
                continue
            if message_type == "FriendMessage" and not self._get("send_private", True):
                continue
            platform_id = str(getattr(row, "platform_id", "") or platform_name)
            is_qq_group = message_type == "GroupMessage" and self._is_aiocqhttp(platform_id)
            group_id = self._qq_group_id(session_id) if is_qq_group else None
            identity = ("qq-group", platform_id, group_id) if group_id else ("umo", umo)
            candidates.append({"umo": umo, "identity": identity, "group_id": group_id,
                               "platform_name": platform_name})

        # Resolve list entries against all aliases before deduplication. A deny on
        # any alias suppresses the physical QQ group, even if another alias is allowed.
        def configured_matches(candidate, values):
            if candidate["umo"] in values:
                return True
            if candidate["group_id"] is None:
                return False
            return any(
                self._configured_group_id(value, candidate["platform_name"]) == candidate["group_id"]
                for value in values
            )

        grouped = {}
        qq_instances = {}
        for candidate in candidates:
            grouped.setdefault(candidate["identity"], []).append(candidate)
            if candidate["group_id"] is not None:
                signature = (candidate["platform_name"], candidate["group_id"])
                qq_instances.setdefault(signature, set()).add(candidate["identity"][1])
        selected = []
        self._target_delivery = {}
        for identity, aliases in grouped.items():
            if any(configured_matches(alias, deny) for alias in aliases):
                continue
            if allow and not any(configured_matches(alias, allow) for alias in aliases):
                continue
            target = min(aliases, key=lambda item: item["umo"])
            signature = (target["platform_name"], target["group_id"])
            selected.append((target["umo"], identity, {
                "key": self._delivery_key(identity),
                "aliases": {alias["umo"] for alias in aliases},
                "legacy_group_signature": signature if target["group_id"] is not None else None,
                "legacy_group_unambiguous": target["group_id"] is not None and len(qq_instances[signature]) == 1,
            }))
        selected.sort(key=lambda item: (item[0], item[1]))
        targets = []
        for umo, _identity, state in selected:
            targets.append(umo)
            self._target_delivery.setdefault(umo, []).append(state)
        return targets

    @staticmethod
    def _parse_umo(umo):
        if not isinstance(umo, str):
            return None
        parts = umo.split(":", 2)
        if len(parts) != 3 or not all(parts) or parts[1] not in {"GroupMessage", "FriendMessage"}:
            return None
        return parts[0], parts[1], parts[2]

    def _is_aiocqhttp(self, platform_id):
        getter = getattr(self.context, "get_platform_inst", None)
        if not callable(getter):
            return False
        try:
            platform = getter(platform_id)
            meta = platform.meta() if platform else None
            return getattr(meta, "name", None) == "aiocqhttp"
        except Exception:
            return False

    @staticmethod
    def _qq_group_id(session_id):
        # AstrBot's aiocqhttp adapter uses sender_group for unique sessions and
        # sends to the final underscore-delimited component.
        group_id = str(session_id).rsplit("_", 1)[-1]
        return group_id if re.fullmatch(r"\d+", group_id) else None

    @classmethod
    def _configured_group_id(cls, umo, platform_name):
        parsed = cls._parse_umo(umo)
        if not parsed or parsed[0] != platform_name or parsed[1] != "GroupMessage":
            return None
        return cls._qq_group_id(parsed[2])

    @staticmethod
    def _delivery_key(identity):
        if identity[0] == "qq-group":
            return f"physical-qq-group:{identity[1]}:{identity[2]}"
        return identity[1]

    @classmethod
    def _was_delivered(cls, delivered, umo, target_state):
        if target_state.get("key", umo) in delivered or umo in delivered:
            return True
        if delivered.intersection(target_state.get("aliases", set())):
            return True
        signature = target_state.get("legacy_group_signature")
        if not signature or not target_state.get("legacy_group_unambiguous"):
            return False
        platform_name, group_id = signature
        return any(cls._configured_group_id(old_umo, platform_name) == group_id for old_umo in delivered)

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
        return 1 + digest[0] % 5, digest[1] % 6

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
            try:
                seconds_start = int(period.start)
                seconds_end = int(period.end)
            except (AttributeError, TypeError, ValueError):
                continue
            if seconds_start < 0 or seconds_end <= seconds_start:
                continue
            zone = _zone(timezone)
            midnight = datetime.combine(local_date, time.min, tzinfo=zone)
            peak_start = midnight + timedelta(seconds=seconds_start)
            peak_end = midnight + timedelta(seconds=seconds_end)
            midpoint = peak_start + (peak_end - peak_start) / 2
            policy = self.fat_fish.get_wallet_policy(at=midpoint, provider_id=provider_id)
            if not self._is_peak_state(policy):
                continue
            start_jitter, end_jitter = self._stable_jitter(
                local_date.isoformat(), f"{seconds_start}-{seconds_end}",
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

        for window in windows:
            cover_id = f"{local_date.isoformat()}-P{window['index']:02d}"
            peak_duration = int((window["peak_end"] - window["peak_start"]).total_seconds() // 60)
            primary_activity = self._primary_peak_activity(
                schedule, local_date, timezone, window["peak_start"], window["peak_end"]
            )
            context = {
                "activity_id": cover_id,
                "peak_duration_minutes": peak_duration,
                "peak_start": window["peak_start"].isoformat(),
                "peak_end": window["peak_end"].isoformat(),
                "primary_activity": primary_activity,
                # Keep a concise ordered itinerary when a long single activity is absent.
                # This is sourced entirely from TimeAwareness's existing raw slots.
                "activity_outline": self._peak_activity_outline(
                    schedule, local_date, timezone, window["peak_start"], window["peak_end"]
                ),
            }
            for kind, trigger in (("PEAK_START", window["cover_start"]), ("PEAK_END", window["cover_end"])):
                if trigger + timedelta(seconds=int(self._get("grace_seconds", 60))) < now.astimezone(_zone(timezone)):
                    continue
                entries.append({
                    "id": f"{cover_id}-{kind}",
                    "kind": kind,
                    "activity_id": cover_id,
                    "activity_duration_minutes": (
                        primary_activity["duration_minutes"] if primary_activity else None
                    ),
                    "trigger_at": trigger.isoformat(),
                    "message": "",
                    "sent": False,
                    "delivered_umos": [],
                    "activity_context": context,
                })
        entries.sort(key=lambda item: item["trigger_at"])
        return entries

    def _primary_peak_activity(self, schedule, local_date, timezone, peak_start, peak_end):
        candidates = []
        zone = _zone(timezone)
        peak_duration = peak_end - peak_start
        minimum_overlap = max(timedelta(minutes=30), peak_duration / 2)
        for index, slot in enumerate(schedule.get("slots", [])):
            if not isinstance(slot, dict):
                continue
            start, start_next_day = self._clock(slot.get("start"))
            end, end_next_day = self._clock(slot.get("end"))
            name, state = str(slot.get("name", "")).strip(), str(slot.get("state", "")).strip()
            if start is None or not (name or state):
                continue
            activity_start = datetime.combine(local_date + timedelta(days=int(start_next_day)), start, tzinfo=zone)
            if end is None:
                continue
            activity_end = datetime.combine(local_date + timedelta(days=int(end_next_day)), end, tzinfo=zone)
            if not end_next_day and activity_end <= activity_start:
                activity_end += timedelta(days=1)
            overlap = min(activity_end, peak_end) - max(activity_start, peak_start)
            activity_duration = activity_end - activity_start
            if overlap < minimum_overlap:
                continue
            candidates.append((overlap, -index, {
                "name": name, "state": state,
                "start": str(slot.get("start", "")), "end": str(slot.get("end", "")),
                "duration_minutes": int(activity_duration.total_seconds() // 60),
            }))
        return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    def _peak_activity_outline(self, schedule, local_date, timezone, peak_start, peak_end, limit=6):
        """Return a small chronological itinerary of real slots overlapping a peak."""
        zone = _zone(timezone)
        candidates = []
        for index, slot in enumerate(schedule.get("slots", [])):
            if not isinstance(slot, dict):
                continue
            start, start_next_day = self._clock(slot.get("start"))
            end, end_next_day = self._clock(slot.get("end"))
            name, state = str(slot.get("name", "")).strip(), str(slot.get("state", "")).strip()
            if start is None or end is None or not (name or state):
                continue
            activity_start = datetime.combine(
                local_date + timedelta(days=int(start_next_day)), start, tzinfo=zone
            )
            activity_end = datetime.combine(
                local_date + timedelta(days=int(end_next_day)), end, tzinfo=zone
            )
            if not end_next_day and activity_end <= activity_start:
                activity_end += timedelta(days=1)
            overlap_seconds = (
                min(activity_end, peak_end) - max(activity_start, peak_start)
            ).total_seconds()
            if overlap_seconds <= 0:
                continue
            candidates.append((overlap_seconds, index, {
                "name": name, "state": state,
                "start": str(slot.get("start", "")),
                "end": str(slot.get("end", "")),
                "overlap_minutes": max(1, int(overlap_seconds // 60)),
            }))

        # Prefer substantial overlaps, then keep their original time order.
        chosen = sorted(candidates, key=lambda item: (-item[0], item[1]))[:limit]
        chosen.sort(key=lambda item: item[1])
        return [item[2] for item in chosen]

    def _prompt_lines(self, entries, schedule, day_kind):
        lines = []
        for entry in entries:
            item = {
                "id": entry["id"],
                "kind": entry["kind"],
                "trigger_at": entry["trigger_at"],
                "name": entry.get("name", ""),
                "state": entry.get("state", ""),
                "time_segment": [entry.get("slot_start", ""), entry.get("slot_end", "")],
                "activity_context": entry.get("activity_context", {}),
            }
            lines.append(json.dumps(item, ensure_ascii=False))
        instructions = (
            f"目标日期 {schedule.get('local_date', '')}，日期性质 {day_kind}。"
            "为所有 ID 一次性生成消息，输出 JSON 对象且键为 ID。"
            "保留默认 Persona 的活泼、随性、吐槽和情绪变化，像本人随口发消息，不要写成通知或固定模板。"
            "每条都必须独立可懂：没看过前文的群友也要能知道具体在做什么；不要只写情绪、去/回/结束等空泛结论。"
            "NORMAL 必须依据给出的 name、state 和 time_segment 明确说出原日程中的事情；可自然表达情绪，但不得补造地点、人物、原因、结果或与日程矛盾。"
            "晚间和凌晨遇到真实的看电影、夜市、演出、宵夜、朋友聚会、游戏等活动时，可更兴奋、好奇、爱玩一点；"
            "不要默认23点就必须睡觉，也绝不能为制造夜生活而补造日程中没有的活动。"
            "PEAK_START 与同 activity_id 的 PEAK_END 共享同一份事实：开始说将去/开始做什么，结束说这件事做完了。"
            "若 primary_activity 存在，以该活动为主；primary_activity 的 duration_minutes 必须与 peak_duration_minutes 相称，不要把短暂子活动说成覆盖整个高峰。"
            "若 primary_activity 为 null 但 activity_outline 非空，按 outline 的真实时间顺序概括几件相连的事，"
            "例如先上课后去看展，不能假装它们是一项持续数小时的活动；前后两条消息必须对应这段真实行程。"
            "PEAK_START 是离开聊天前的生活分享：在不编造事实的前提下，应优先挑出 outline 里1–3件最能解释这段时间为何不在线的实质活动，"
            "让群友知道接下来大概会去做什么；不要把刷手机、发呆、普通吃饭这类短暂过渡当成整段高峰的主解释，除非没有更实质的安排。"
            "PEAK_END 要自然回扣同一份 outline，概括刚做完的主要事情，不需要把每个小步骤逐项复述。"
            "只有 primary_activity 为 null 且 activity_outline 也为空，才可以自然描述暂时有事要忙和忙完了。"
            "所有情况下都不得猜测或虚构事实，不能只写溜了、回来了、忙一阵等无背景的空话。"
            "不要为了交代背景而过度解释；保持口语、简短、有变化。"
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

    async def raw_cycle(self, now=None):
        """Stitch TimeAwareness natural-day snapshots into Xiaoman's rolling 24h life day."""
        now = self._now(now)
        window = self.day_adapter.rolling_day_window(now)
        start = window["start"]
        end = window["end"]
        targets = await self.targets()
        dates = [start.date()]
        if end.date() != start.date():
            dates.append(end.date())

        snapshots = []
        slots = []
        for target_date in dates:
            schedule = await self._read_date(target_date, targets, now)
            if schedule is None:
                continue
            snapshot_id = str(schedule.get("snapshot_id", "") or "")
            if snapshot_id:
                snapshots.append(snapshot_id)
            timezone = str(schedule.get("timezone") or "Asia/Shanghai")
            zone = _zone(timezone)
            local_date = date.fromisoformat(str(schedule.get("local_date") or target_date.isoformat()))
            for slot in schedule.get("slots", []):
                if not isinstance(slot, dict):
                    continue
                slot_start, start_next_day = self._clock(slot.get("start"))
                slot_end, end_next_day = self._clock(slot.get("end"))
                if slot_start is None or slot_end is None:
                    continue
                absolute_start = datetime.combine(
                    local_date + timedelta(days=int(start_next_day)),
                    slot_start,
                    tzinfo=zone,
                )
                absolute_end = datetime.combine(
                    local_date + timedelta(days=int(end_next_day)),
                    slot_end,
                    tzinfo=zone,
                )
                if not end_next_day and absolute_end <= absolute_start:
                    absolute_end += timedelta(days=1)
                if absolute_start >= end or absolute_end <= start:
                    continue
                clipped_start = max(absolute_start, start.astimezone(zone))
                clipped_end = min(absolute_end, end.astimezone(zone))
                copied = dict(slot)
                copied["start_at"] = clipped_start.isoformat(timespec="minutes")
                copied["end_at"] = clipped_end.isoformat(timespec="minutes")
                copied["calendar_date"] = local_date.isoformat()
                slots.append(copied)

        slots.sort(key=lambda item: item.get("start_at", ""))
        return {
            "generation_time": window.get("raw", ""),
            "boundary_clock": window.get("clock", ""),
            "window_start": start.isoformat(timespec="minutes"),
            "window_end": end.isoformat(timespec="minutes"),
            "snapshots": snapshots,
            "complete": len(snapshots) == len(dates),
            "slots": slots,
        }

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

    async def simulate_time(self, hhmm, now=None):
        """Run the normal due-send path against an isolated copy of today's plan."""
        current = self._now(now)
        if (not isinstance(hhmm, str) or len(hhmm) != 5 or hhmm[2] != ":"
                or not hhmm[:2].isdigit() or not hhmm[3:].isdigit()):
            return None, "时间格式应为 HH:MM。"
        try:
            simulated_clock = time.fromisoformat(hhmm)
        except ValueError:
            return None, "无效的模拟时间。"
        plan = self.plan_for_date(current.date())
        if not plan:
            return None, "当天没有已生成的 EFFECTIVE 日程。"
        timezone = str(plan.get("timezone") or "Asia/Shanghai")
        simulated_now = datetime.combine(current.date(), simulated_clock, tzinfo=_zone(timezone))
        isolated_plan = copy.deepcopy(plan)
        result = await self.send_due(simulated_now, plans=[isolated_plan], force_send=True)
        result["simulated_at"] = simulated_now.isoformat()
        if not result["hit_event_ids"] and not result["expired_event_ids"]:
            result["reason"] = "该时间没有到期事件。"
        elif not result["hit_event_ids"] and result["expired_event_ids"]:
            result["reason"] = "事件已超过宽限期，未发送。"
        elif not result["target_count"]:
            result["reason"] = "没有符合群聊/私聊、允许名单和拒绝名单配置的投递目标。"
        elif result["failures"]:
            result["reason"] = "部分目标发送失败；详见失败明细。"
        return result, ""

    async def send_due(self, now=None, *, plans=None, force_send=False):
        persist = plans is None
        targets = await self.targets()
        now = self._now(now)
        active_plans = self._plans().values() if persist else plans
        result = {
            "hit_event_ids": [], "expired_event_ids": [], "target_count": len(targets),
            "success_count": 0, "failures": [],
        }
        if targets:
            from astrbot.api.event import MessageChain
        for plan in active_plans:
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
                    result["expired_event_ids"].append(entry.get("id", ""))
                    if persist:
                        self._save()
                    continue
                if local_now > trigger + timedelta(seconds=int(self._get("grace_seconds", 60))):
                    entry["expired"] = True
                    result["expired_event_ids"].append(entry.get("id", ""))
                    if persist:
                        self._save()
                    continue
                if local_now < trigger:
                    continue
                result["hit_event_ids"].append(entry.get("id", ""))
                if not targets:
                    continue
                if self._get("dry_run", False) and not force_send:
                    if not entry.get("dry_run_logged"):
                        logger.info("Schedule broadcast dry-run %s: %s", entry["id"], entry["message"])
                        entry["dry_run_logged"] = True
                        if persist:
                            self._save()
                    continue
                delivered = set(entry.get("delivered_umos", []))
                target_occurrences = {}
                for umo in targets:
                    occurrence = target_occurrences.get(umo, 0)
                    target_occurrences[umo] = occurrence + 1
                    states = self._target_delivery.get(umo, [])
                    target_state = states[occurrence] if occurrence < len(states) else {}
                    # Old states stored raw UMOs. Match any current alias of a
                    # physical target so unique_session history remains valid.
                    if self._was_delivered(delivered, umo, target_state):
                        continue
                    try:
                        sent = await self.context.send_message(umo, MessageChain().message(entry["message"]))
                        if sent is False:
                            result["failures"].append({
                                "entry_id": entry.get("id", ""), "umo": umo,
                                "reason": "send_message returned False",
                            })
                            continue
                        result["success_count"] += 1
                        delivered.add(target_state.get("key", umo))
                        entry["delivered_umos"] = sorted(delivered)
                        if persist:
                            self._save()
                    except Exception as exc:
                        result["failures"].append({
                            "entry_id": entry.get("id", ""), "umo": umo,
                            "reason": str(exc) or type(exc).__name__,
                        })
                        logger.warning("Schedule broadcast send failed", exc_info=True)
                target_occurrences = {}
                all_delivered = bool(targets)
                for umo in targets:
                    occurrence = target_occurrences.get(umo, 0)
                    target_occurrences[umo] = occurrence + 1
                    states = self._target_delivery.get(umo, [])
                    target_state = states[occurrence] if occurrence < len(states) else {}
                    if not self._was_delivered(delivered, umo, target_state):
                        all_delivered = False
                        break
                entry["sent"] = all_delivered
                if persist:
                    self._save()
        result["failure_count"] = len(result["failures"])
        return result

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
