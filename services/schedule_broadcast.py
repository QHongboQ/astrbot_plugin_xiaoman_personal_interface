"""Xiaoman-owned rolling life-day planner and deterministic message executor."""
from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, time, timezone as datetime_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .fat_fish_bridge import FatFishBridge
from .time_awareness_adapter import TimeAwarenessAdapter

PLANNER_VERSION = "0.8.2"
SCHEMA_VERSION = 2
TIMELINE_CATEGORIES = {
    "sleep", "rest", "meal", "travel", "school", "creative", "social",
    "entertainment", "outdoor", "shopping", "errand", "mixed", "other",
}


def _zone(name):
    try:
        return ZoneInfo(str(name))
    except Exception:
        fixed_name = str(name).removeprefix("UTC")
        if re.fullmatch(r"[+-]\d{2}:\d{2}", fixed_name):
            sign = 1 if fixed_name[0] == "+" else -1
            hours, minutes = map(int, fixed_name[1:].split(":"))
            return datetime_timezone(sign * timedelta(hours=hours, minutes=minutes))
        if str(name) in {"UTC", "Etc/UTC"}:
            return datetime_timezone.utc
        if str(name) == "Asia/Shanghai":
            return datetime_timezone(timedelta(hours=8), "Asia/Shanghai")
        raise


def _absolute(value):
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except ValueError:
        return None


class ScheduleBroadcastService:
    """Own one complete life-day timeline; send only its persisted messages."""

    def __init__(self, context, config, data_dir, *, time_awareness=None, fat_fish=None):
        self.context = context
        self.config = config or {}
        self.cfg = self.config.get("schedule_broadcast", {}) or {}
        self.path = Path(data_dir) / "life_day_plans.json"
        self.legacy_path = Path(data_dir) / "schedule_broadcast_state.json"
        self.state = {"schema_version": SCHEMA_VERSION, "plans": {}, "generation_attempts": {}}
        self.day_adapter = time_awareness or TimeAwarenessAdapter(context)
        self.fat_fish = fat_fish or FatFishBridge(context, self.day_adapter)
        self.last_error = ""
        self._task = None
        self._generation_lock = asyncio.Lock()
        self._target_delivery = {}
        self._load()

    def _get(self, key, default=None):
        return self.cfg.get(key, default)

    def _int(self, key, default, minimum=0):
        try:
            value = int(self._get(key, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        return max(minimum, value)

    def _bool(self, key, default):
        value = self._get(key, default)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"false", "0", "no", "off", "否"}:
                return False
            if normalized in {"true", "1", "yes", "on", "是"}:
                return True
        return bool(value)

    def _activity_pool(self):
        configured = self._get("activity_pool", []) or []
        if isinstance(configured, str):
            configured = [configured]
        result = []
        seen = set()
        for item in configured:
            if isinstance(item, dict):
                name = str(item.get("name", "") or "").strip()
                weight = item.get("weight", 1)
            else:
                text = str(item or "").strip()
                if not text:
                    continue
                if "," in text:
                    name, raw_weight = text.rsplit(",", 1)
                    name = name.strip()
                    try:
                        weight = int(raw_weight.strip())
                    except (TypeError, ValueError, OverflowError):
                        weight = 1
                else:
                    name, weight = text, 1
            if not name or name in seen:
                continue
            try:
                weight = int(weight)
            except (TypeError, ValueError, OverflowError):
                weight = 1
            result.append({"name": name, "weight": max(1, min(10, weight))})
            seen.add(name)
        return result

    def _activity_density(self):
        value = str(self._get("activity_density", "balanced") or "balanced").strip().lower()
        return value if value in {"relaxed", "balanced", "busy"} else "balanced"

    def _sleep_target_hours(self):
        try:
            value = int(self._get("sleep_target_hours", 8))
        except (TypeError, ValueError, OverflowError):
            value = 8
        return max(4, min(12, value))

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION
                    and isinstance(data.get("plans"), dict)):
                self.state = data
                self.state.setdefault("generation_attempts", {})
                failed = [row for row in self.state["generation_attempts"].values()
                          if isinstance(row, dict) and row.get("status") == "failed"]
                if failed:
                    failed.sort(key=lambda row: row.get("at", ""), reverse=True)
                    self.last_error = str(failed[0].get("error", ""))
                return
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Life-day plan store load failed", exc_info=True)
        # Never interpret 0.7.x natural-date plans as v0.8 life-day plans.
        if self.legacy_path.exists():
            self.state["legacy_state_path"] = str(self.legacy_path)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _now(self, value=None):
        now = value or self.day_adapter.current_time()
        return now if now.tzinfo else now.astimezone()

    def _plans(self):
        plans = self.state.get("plans")
        if not isinstance(plans, dict):
            self.state["plans"] = {}
        return self.state["plans"]

    @staticmethod
    def _plan_key(start_at):
        return start_at.isoformat()

    def life_day(self, at=None):
        return self.day_adapter.life_day_window(self._now(at))

    def next_life_day(self, at=None):
        current = self.life_day(at)
        start = current["end"]
        return {**current, "start": start, "end": start + timedelta(days=1)}

    async def targets(self):
        try:
            rows = self.context.conversation_manager.get_conversations()
            rows = await rows
        except Exception:
            return []
        allow = set(self._get("allowlist_umos", []) or [])
        deny = set(self._get("denylist_umos", []) or [])
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
            group_id = (self._qq_group_id(session_id)
                        if message_type == "GroupMessage" and self._is_aiocqhttp(platform_id) else None)
            identity = ("qq-group", platform_id, group_id) if group_id else ("umo", umo)
            candidates.append({"umo": umo, "identity": identity, "group_id": group_id,
                               "platform_name": platform_name})

        def matches(candidate, values):
            if candidate["umo"] in values:
                return True
            group_id = candidate["group_id"]
            return bool(group_id and any(
                self._configured_group_id(value, candidate["platform_name"]) == group_id
                for value in values))

        grouped, qq_instances = {}, {}
        for candidate in candidates:
            grouped.setdefault(candidate["identity"], []).append(candidate)
            if candidate["group_id"]:
                sig = (candidate["platform_name"], candidate["group_id"])
                qq_instances.setdefault(sig, set()).add(candidate["identity"][1])
        selected, self._target_delivery = [], {}
        for identity, aliases in grouped.items():
            if any(matches(alias, deny) for alias in aliases):
                continue
            if allow and not any(matches(alias, allow) for alias in aliases):
                continue
            target = min(aliases, key=lambda item: item["umo"])
            signature = (target["platform_name"], target["group_id"])
            selected.append((target["umo"], identity, {
                "key": self._delivery_key(identity),
                "aliases": {alias["umo"] for alias in aliases},
                "legacy_group_signature": signature if target["group_id"] else None,
                "legacy_group_unambiguous": bool(target["group_id"] and len(qq_instances[signature]) == 1),
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
        value = str(session_id).rsplit("_", 1)[-1]
        return value if re.fullmatch(r"\d+", value) else None

    @classmethod
    def _configured_group_id(cls, umo, platform_name):
        parsed = cls._parse_umo(umo)
        if not parsed or parsed[0] != platform_name or parsed[1] != "GroupMessage":
            return None
        return cls._qq_group_id(parsed[2])

    @staticmethod
    def _delivery_key(identity):
        return f"physical-qq-group:{identity[1]}:{identity[2]}" if identity[0] == "qq-group" else identity[1]

    @classmethod
    def _was_delivered(cls, delivered, umo, target_state):
        if target_state.get("key", umo) in delivered or umo in delivered:
            return True
        if delivered.intersection(target_state.get("aliases", set())):
            return True
        signature = target_state.get("legacy_group_signature")
        if not signature or not target_state.get("legacy_group_unambiguous"):
            return False
        return any(cls._configured_group_id(old, signature[0]) == signature[1] for old in delivered)

    async def _provider(self, targets):
        provider = str(self._get("provider_id", "") or "")
        if provider or not targets:
            return provider
        try:
            return str(await self.context.get_current_chat_provider_id(umo=targets[0]) or "")
        except Exception:
            return ""

    def _calendar_days(self, planner_context):
        return planner_context.get("calendar_days", []) if isinstance(planner_context, dict) else []

    @staticmethod
    def _merge_protected(windows, life_start, life_end, before, after):
        expanded = []
        for source in windows:
            start = max(source["start_at"] - timedelta(minutes=before), life_start)
            end = min(source["end_at"] + timedelta(minutes=after), life_end)
            if start < end:
                expanded.append({"start_at": start, "end_at": end,
                                 "source_peak_start": source["source_peak_start"],
                                 "source_peak_end": source["source_peak_end"]})
        expanded.sort(key=lambda item: item["start_at"])
        merged = []
        for window in expanded:
            if merged and window["start_at"] <= merged[-1]["end_at"]:
                merged[-1]["end_at"] = max(merged[-1]["end_at"], window["end_at"])
                merged[-1]["source_peak_start"] = min(merged[-1]["source_peak_start"], window["source_peak_start"])
                merged[-1]["source_peak_end"] = max(merged[-1]["source_peak_end"], window["source_peak_end"])
            else:
                merged.append(dict(window))
        for index, item in enumerate(merged, 1):
            item["id"] = f"P{index:02d}"
            for key in ("start_at", "end_at", "source_peak_start", "source_peak_end"):
                item[key] = item[key].isoformat()
        return merged

    @staticmethod
    def _free_complement(life_start, life_end, protected):
        free, cursor = [], life_start
        for index, item in enumerate(protected, 1):
            start, end = _absolute(item["start_at"]), _absolute(item["end_at"])
            if cursor < start:
                free.append({"id": f"F{len(free)+1:02d}", "start_at": cursor.isoformat(), "end_at": start.isoformat()})
            cursor = max(cursor, end)
        if cursor < life_end:
            free.append({"id": f"F{len(free)+1:02d}", "start_at": cursor.isoformat(), "end_at": life_end.isoformat()})
        return free

    @staticmethod
    def _legacy_category(row):
        """Name fallback is only for persisted plans written before category existed."""
        name = str(row.get("name", "")).lower()
        if any(word in name for word in ("sleep", "睡", "补觉", "赖床")):
            return "sleep"
        if any(word in name for word in ("休息", "发呆", "躺", "什么都不")):
            return "rest"
        if any(word in name for word in ("吃", "饭", "餐", "宵夜")):
            return "meal"
        return "other"

    @classmethod
    def _history_category(cls, row, *, legacy=False):
        category = row.get("category")
        if category in TIMELINE_CATEGORIES:
            return category
        if legacy and not category:
            return cls._legacy_category(row)
        return "other"

    @staticmethod
    def _minutes(start, end):
        if not start or not end or end <= start:
            return 0
        return int((end - start).total_seconds() // 60)

    def _history(self, life_start, recent_days, enabled):
        if not enabled or recent_days <= 0:
            return []
        completed = []
        for plan in self._plans().values():
            end = _absolute(plan.get("life_day_end", ""))
            if plan.get("status") != "complete" or not end or end > life_start:
                continue
            timeline = plan.get("timeline", [])
            planner_version = str(plan.get("planner_version", ""))
            legacy_categories = planner_version in {"", "0.8.0", "0.8.1"}
            names = [row.get("name", "") for row in timeline if row.get("name")]
            categorized = [(row, self._history_category(row, legacy=legacy_categories))
                           for row in timeline]
            sleep = [(row, category) for row, category in categorized if category == "sleep"]
            sleep_minutes = sum(self._minutes(_absolute(row.get("start_at")),
                                              _absolute(row.get("end_at")))
                                for row, _category in sleep)
            tail = [{"category": category, "name": row.get("name", ""),
                     "start_at": row.get("start_at"), "end_at": row.get("end_at"),
                     "state": row.get("state", "")}
                    for row, category in categorized[-3:]]
            late_night = []
            for row in timeline:
                name = str(row.get("name", ""))
                starts = _absolute(row.get("start_at", ""))
                if (starts and (starts.hour >= 22 or starts.hour < 4)) or any(
                        word in name.lower() for word in ("night", "夜", "凌晨", "宵夜")):
                    late_night.append(name)
            previous_end = _absolute(plan.get("life_day_end", ""))
            final = categorized[-1] if categorized else (None, "other")
            ended_awake = bool(final[1] != "sleep")
            ended_awake_at_boundary = bool(
                ended_awake and previous_end
                and _absolute(final[0].get("end_at")) == previous_end)
            continuous_sleep_rows = []
            cursor = previous_end
            for row, category in reversed(categorized):
                row_start, row_end = _absolute(row.get("start_at")), _absolute(row.get("end_at"))
                if category != "sleep" or not cursor or row_end != cursor:
                    break
                continuous_sleep_rows.append(row)
                cursor = row_start
            continuous_sleep_start = cursor if continuous_sleep_rows else None
            sleep_before_boundary = self._minutes(continuous_sleep_start, previous_end)
            target_minutes = self._sleep_target_hours() * 60
            suggested_wake = None
            if continuous_sleep_start:
                suggested_wake = continuous_sleep_start + timedelta(minutes=target_minutes)
            elif ended_awake_at_boundary and previous_end:
                suggested_wake = previous_end + timedelta(minutes=target_minutes)
            completed.append({"life_day_start": plan.get("life_day_start"),
                              "daily_theme": plan.get("daily_theme", ""),
                              "daily_style": plan.get("daily_style", ""),
                              "major_activities": names,
                              "categories_used": sorted({category for _row, category in categorized}),
                              "sleep_period": [{"start_at": row.get("start_at"), "end_at": row.get("end_at"),
                                                "minutes": self._minutes(_absolute(row.get("start_at")),
                                                                         _absolute(row.get("end_at")))}
                                               for row, _category in sleep],
                              "previous_sleep_minutes": sleep_minutes,
                              "sleep_debt_minutes": max(0, target_minutes - sleep_minutes),
                              "previous_tail": tail,
                              "sleep_continuity": {
                                  "target_sleep_hours": self._sleep_target_hours(),
                                  "previous_sleep_minutes": sleep_minutes,
                                  "previous_life_day_ended_awake": ended_awake,
                                  "ended_awake_at_boundary": ended_awake_at_boundary,
                                  "continuous_sleep_at_boundary": bool(continuous_sleep_rows),
                                  "continuous_sleep_start_at": continuous_sleep_start.isoformat()
                                  if continuous_sleep_start else None,
                                  "sleep_minutes_before_boundary": sleep_before_boundary,
                                  "suggested_wake_not_before": suggested_wake.isoformat()
                                  if suggested_wake else None,
                                  "recovery_sleep_needed": bool(
                                      sleep_before_boundary < target_minutes
                                      if continuous_sleep_rows else ended_awake_at_boundary
                                      or sleep_minutes < target_minutes),
                              },
                              "late_night_behavior": late_night,
                              "previous_final_events": names[-3:]})
        completed.sort(key=lambda row: row.get("life_day_start", ""), reverse=True)
        return completed[:recent_days]

    async def planner_input(self, start_at, provider_id=""):
        end_at = start_at + timedelta(days=1)
        planner_context = await self.day_adapter.planner_context(start_at, end_at)
        boundary = self.day_adapter.get_generation_boundary()
        window = self.day_adapter.life_day_window(start_at)
        timezone_name = (window.get("timezone") or getattr(start_at.tzinfo, "key", None)
                         or str(start_at.tzinfo))
        days = self._calendar_days(planner_context)
        source_peaks = self.fat_fish.effective_peak_windows(
            start_at, end_at, provider_id, days)
        protected = self._merge_protected(
            source_peaks, start_at, end_at,
            self._int("peak_guard_before_minutes", 5),
            self._int("peak_guard_after_minutes", 5))
        free = self._free_complement(start_at, end_at, protected)
        adaptive = planner_context.get("adaptive", {})
        history = self._history(start_at, int(adaptive.get("recent_days") or 0),
                                bool(adaptive.get("state_continuity_enabled", True)))
        latest = history[0] if history else None
        sleep_continuity = (latest.get("sleep_continuity") if latest else None) or {
            "target_sleep_hours": self._sleep_target_hours(),
            "previous_sleep_minutes": None,
            "previous_life_day_ended_awake": None,
            "ended_awake_at_boundary": None,
            "continuous_sleep_at_boundary": False,
            "continuous_sleep_start_at": None,
            "sleep_minutes_before_boundary": 0,
            "suggested_wake_not_before": None,
            "recovery_sleep_needed": False,
        }
        continuity = {
            "has_previous_life_day": bool(latest),
            "previous_tail": latest.get("previous_tail", []) if latest else [],
            "accumulated_sleep_debt_minutes": sum(
                row.get("sleep_debt_minutes", 0) for row in history),
            "sleep_continuity": sleep_continuity,
            "history_note": ("按可用的既往生命日延续；没有依据的过往事件不得当作事实。"
                             if latest else "没有可用的既往小满生命日历史；不得编造昨晚/前几天发生过的事实。"),
        }
        persona = ""
        if planner_context.get("use_persona", True):
            try:
                value = await self.context.persona_manager.get_default_persona_v3()
                persona = str(value.get("prompt", "") or "") if isinstance(value, dict) else str(getattr(value, "prompt", "") or "")
            except Exception:
                persona = ""
        return {
            "life_day": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat(),
                         "boundary_clock": boundary["clock"]},
            "timezone": str(timezone_name),
            "calendar_days": days,
            "free_windows": free,
            "protected_windows": protected,
            "worldview": planner_context.get("worldview", ""),
            "persona": persona,
            "theme_pool": planner_context.get("theme_pool", []),
            "style_pool": planner_context.get("style_pool", []),
            "allow_custom_theme": planner_context.get("allow_custom_theme", True),
            "weather": planner_context.get("weather", []),
            "recent_life_days": history,
            "activity_pool": self._activity_pool(),
            "activity_pool_allow_custom": self._bool("activity_pool_allow_custom", True),
            "activity_density": self._activity_density(),
            "sleep_policy": {"target_hours": self._sleep_target_hours()},
            "continuity": continuity,
        }

    def _planner_prompt(self, planner_input):
        guidance = (
            "你是林小满的生活日规划器。规划的是完整 life_day，不是自然日；一次性从头安排到尾。"
            "NORMAL 事件只能位于一个 free_window 内，且全部 NORMAL 须首尾相接完整填满每个 free_window。"
            "每个 protected_window 恰好一个 BRIDGE，start_at/end_at 必须精确照抄保护窗边界，不能拆分顶层事件。"
            "BRIDGE 内部阶段可以写在 state。最终 timeline 按时间排序、连续无缝、无重叠，覆盖 life_day 起止。"
            "每个 NORMAL 产生 broadcast_message；每个 BRIDGE 产生 enter_message 和 exit_message。"
            "桥接活动须和前后事件一起形成因果连续的一天，不要把分段当成互不相关的活动。"
            "林小满是课表相对宽松的艺术专业大学生；上课只是可能选项，不得默认课堂/食堂/自习/宿舍是每日主轴，也允许整天不上课。"
            "activity_pool 是小满对具体活动的加权偏好，权重只表示相对偏好，不是精确概率；现实、天气、精力、睡眠和近期重复式样优先于权重。"
            "activity_pool_allow_custom=true 时可自然安排池外活动；为 false 时，主要休闲/社交活动应来自活动池，除非世界观、日历或既有连续性要求其他安排。不得重复实现 theme_pool：主题池决定日子是什么感觉，活动池提供具体可做的事。"
            "activity_density 是软目标：relaxed 约1-2项主要活动并留大量自由/休息时间；balanced 约2-3项；busy 约3-4项。不得为凑数量制造活动。完整覆盖24小时不代表必须保持忙碌。睡觉、躺着、打游戏、看视频、发呆、休息、聊天、通勤和慢慢吃饭都可以是长 NORMAL 区块。"
            "在 free_window 内，较长睡眠通常应作为独立 category=sleep 的 NORMAL 事件，不要把补觉/回笼觉藏在其他事件的 state。protected_window 的 BRIDGE 必须保持一个不可拆分的顶层事件，因此是睡眠事件独立成项规则的例外：若 BRIDGE 跨越自然的睡眠→醒来→慢启动，可在同一 BRIDGE.state 中写明内部阶段，并通常使用 category=mixed；不要为了避免 mixed 而提前叫醒小满，也不得把一个 BRIDGE 拆成多个顶层事件。如果整个 BRIDGE 确实都在睡觉，category=sleep 仍然合适。"
            "sleep_policy.target_hours 是一般睡眠目标，不是硬性医学规则；偶尔可少睡或多睡，但既往睡眠不足会降低次日活动强度。"
            "如果上一生命日以睡眠结束，且边界前连续睡眠尚未达到目标，通常应把同一睡眠延续到新生命日；不得仅为了腾出活动时间而早起，也不得编造早课、作业、预约或截止日期来解释中断睡眠。若前一生命日很晚仍清醒/在边界时清醒，下一日通常先安排睡眠或恢复。上一日睡眠明显不足时降低活动密度；continuity.accumulated_sleep_debt_minutes 汇总近期睡眠缺口，累积缺口也必须影响后续恢复与活动强度。保护窗 BRIDGE 可以继续同一段睡眠/休息/慢启动，不要为了保护窗凭空制造早课或外出活动。"
            "只根据 continuity 和 recent_life_days 中明确提供的历史延续。若无可用历史，必须视为没有已知的前夜/前几日事件；不得编造‘昨晚通宵赶作业’等事实，只生成合理的起始状态。近期主要活动、类别、主题、风格和睡眠只用于避免重复及维持连续性。"
            "优先遵循 worldview、theme_pool、style_pool、天气和真实日历。不要每天塞满高强度活动。人物、地点、天气影响、消费和结果不得无依据编造。"
            "只输出 JSON，不要 Markdown。字段：daily_theme,daily_style,timeline。时间为带时区 ISO 8601。"
            "timeline 每项都必须包含 category，且只能是 sleep,rest,meal,travel,school,creative,social,entertainment,outdoor,shopping,errand,mixed,other 之一。"
            "NORMAL 项字段：id,kind=NORMAL,category,start_at,end_at,name,state,broadcast_message。"
            "BRIDGE 项字段：id,kind=BRIDGE,category,start_at,end_at,name,state,source_peak_start,source_peak_end,enter_message,exit_message；source_peak_* 原样照抄对应窗口。"
            f"每条消息不得超过 {self._int('max_message_chars', 80, 1)} 个字符。"
        )
        extra = str(self._get("planner_prompt", "") or "").strip()
        return guidance + (f"\n补充规划要求：{extra}" if extra else "") + "\nPLANNER_INPUT:\n" + json.dumps(planner_input, ensure_ascii=False, default=str)

    def validate_timeline(self, planner_input, timeline):
        if not isinstance(timeline, list) or not timeline:
            return False, "timeline missing"
        life = planner_input["life_day"]
        start, end = _absolute(life["start_at"]), _absolute(life["end_at"])
        protected, free = planner_input["protected_windows"], planner_input["free_windows"]
        ids, parsed, max_chars = set(), [], self._int("max_message_chars", 80, 1)
        for row in timeline:
            if not isinstance(row, dict) or row.get("kind") not in {"NORMAL", "BRIDGE"}:
                return False, "unknown timeline entry"
            ident = str(row.get("id", ""))
            a, b = _absolute(row.get("start_at")), _absolute(row.get("end_at"))
            if not ident or ident in ids or not a or not b or a >= b:
                return False, "invalid timeline id or interval"
            ids.add(ident)
            if row.get("category") not in TIMELINE_CATEGORIES:
                return False, f"{ident} missing or invalid category"
            if not str(row.get("name", "")).strip():
                return False, f"{ident} missing name"
            if not isinstance(row.get("state"), str):
                return False, f"{ident} missing state"
            if row["kind"] == "NORMAL":
                messages = [row.get("broadcast_message")]
            else:
                messages = [row.get("enter_message"), row.get("exit_message")]
            if any(not isinstance(message, str) or not message.strip() or len(message) > max_chars
                   for message in messages):
                return False, f"{ident} missing or oversized broadcast message"
            parsed.append((a, b, row))
        if parsed != sorted(parsed, key=lambda item: item[0]):
            return False, "timeline is not sorted"
        if parsed[0][0] != start or parsed[-1][1] != end:
            return False, "timeline does not cover life-day boundaries"
        for previous, current in zip(parsed, parsed[1:]):
            if previous[1] != current[0]:
                return False, "timeline has gap or overlap"
        for a, b, row in parsed:
            if row["kind"] == "NORMAL":
                if not any(a >= _absolute(window["start_at"]) and b <= _absolute(window["end_at"])
                           for window in free):
                    return False, f"{row['id']} NORMAL crosses a protected window"
            else:
                matching = [window for window in protected
                            if a == _absolute(window["start_at"]) and b == _absolute(window["end_at"])]
                if len(matching) != 1:
                    return False, f"{row['id']} BRIDGE does not exactly match one protected window"
                source = matching[0]
                if row.get("source_peak_start") != source.get("source_peak_start") or row.get("source_peak_end") != source.get("source_peak_end"):
                    return False, f"{row['id']} source peak boundaries mismatch"
        bridges = [row for _a, _b, row in parsed if row["kind"] == "BRIDGE"]
        if len(bridges) != len(protected):
            return False, "each protected window must have exactly one BRIDGE"
        for window in free:
            a, b = _absolute(window["start_at"]), _absolute(window["end_at"])
            rows = [(x, y, row) for x, y, row in parsed if row["kind"] == "NORMAL" and x >= a and y <= b]
            if not rows or rows[0][0] != a or rows[-1][1] != b:
                return False, f"free window {window['id']} not fully filled"
            if any(left[1] != right[0] for left, right in zip(rows, rows[1:])):
                return False, f"free window {window['id']} has gap or overlap"
        return True, ""

    @staticmethod
    def _derive_deliveries(timeline, old_deliveries=None):
        old = {(row.get("id"), row.get("trigger_at")): row for row in old_deliveries or []}
        old_by_id = {row.get("id"): row for row in old_deliveries or []}
        bridge_ends = {row["end_at"] for row in timeline if row["kind"] == "BRIDGE"}
        events = []
        for row in timeline:
            if row["kind"] == "NORMAL":
                if row["start_at"] in bridge_ends:
                    continue
                events.append({"id": row["id"], "kind": "NORMAL", "timeline_id": row["id"],
                               "trigger_at": row["start_at"], "message": row["broadcast_message"]})
            else:
                events.extend([
                    {"id": row["id"] + "-ENTER", "kind": "BRIDGE_ENTER", "timeline_id": row["id"],
                     "trigger_at": row["start_at"], "message": row["enter_message"]},
                    {"id": row["id"] + "-EXIT", "kind": "BRIDGE_EXIT", "timeline_id": row["id"],
                     "trigger_at": row["end_at"], "message": row["exit_message"]},
                ])
        for event in events:
            previous = old.get((event["id"], event["trigger_at"]))
            if previous is None:
                candidate = old_by_id.get(event["id"], {})
                previous = candidate if candidate.get("sent") or candidate.get("delivered_umos") else {}
            previous = previous or {}
            for key in ("sent", "expired", "delivered_umos", "dry_run_logged"):
                if key in previous:
                    event[key] = copy.deepcopy(previous[key])
            event.setdefault("sent", False)
            event.setdefault("expired", False)
            event.setdefault("delivered_umos", [])
        return events

    async def generate_life_day(self, life_day_start, *, force=False, allow_wallet_bypass=False):
        if isinstance(life_day_start, str):
            life_day_start = datetime.fromisoformat(life_day_start)
        if not life_day_start.tzinfo:
            life_day_start = life_day_start.astimezone()
        key = self._plan_key(life_day_start)
        async with self._generation_lock:
            old = self._plans().get(key)
            if old and old.get("status") == "complete" and not force:
                return {"status": "exists", "start_at": key}
            attempts = self.state.setdefault("generation_attempts", {})
            attempt = attempts.get(key, {})
            retry_seconds = self._int("planner_failure_retry_seconds", 1800)
            if (not force and retry_seconds > 0 and attempt.get("status") == "failed"
                    and (self._now() - datetime.fromisoformat(attempt["at"])).total_seconds() < retry_seconds):
                return {"status": "throttled", "reason": attempt.get("error", "recent planner failure")}
            self.last_error = ""
            wallet_bypass_used = False
            try:
                targets = await self.targets()
                provider = await self._provider(targets)
                if not provider:
                    raise RuntimeError("provider unavailable")
                planner_input = await self.planner_input(life_day_start, provider)
                planner_input = json.loads(json.dumps(planner_input, ensure_ascii=False, default=str))
                persona_prompt = planner_input.get("persona", "")
                prompt = self._planner_prompt(planner_input)
                # Fat Fish gates message events, not this internal planner call.
                # Recheck its read-only policy at call time; force regeneration
                # must not bypass an active wallet block either.
                wallet_policy = self.fat_fish.get_wallet_policy(
                    at=self._now(), provider_id=provider)
                if wallet_policy.get("found") and not wallet_policy.get("allowed", True):
                    wallet_bypass_used = bool(
                        allow_wallet_bypass
                        and self._get("admin_regenerate_bypass_fat_fish", True)
                        and wallet_policy.get("admins_bypass", True)
                        and wallet_policy.get("provider_resolved", False))
                    if not wallet_bypass_used:
                        return {"status": "deferred", "reason": "blocked_by_fat_fish",
                                "policy": wallet_policy, "wallet_bypass_used": False}
                    logger.info("Xiaoman planner manual admin bypass used for Fat Fish wallet gate")
                response = await self.context.llm_generate(
                    chat_provider_id=provider, prompt=prompt,
                    system_prompt=persona_prompt, tools=None)
                raw = getattr(response, "completion_text", None) or getattr(response, "text", None) or str(response)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("planner response must be a JSON object")
                timeline = result.get("timeline")
                if not str(result.get("daily_theme", "")).strip() or not str(result.get("daily_style", "")).strip():
                    raise ValueError("planner response missing daily_theme or daily_style")
                valid, reason = self.validate_timeline(planner_input, timeline)
                if not valid:
                    raise ValueError("planner timeline rejected: " + reason)
                old_deliveries = old.get("deliveries", []) if old else []
                plan = {
                    "schema_version": SCHEMA_VERSION,
                    "planner_version": PLANNER_VERSION,
                    "life_day_start": planner_input["life_day"]["start_at"],
                    "life_day_end": planner_input["life_day"]["end_at"],
                    "timezone": planner_input.get("timezone", str(life_day_start.tzinfo)),
                    "daily_theme": str(result.get("daily_theme", "")).strip(),
                    "daily_style": str(result.get("daily_style", "")).strip(),
                    "timeline": timeline,
                    "deliveries": self._derive_deliveries(timeline, old_deliveries),
                    "status": "complete",
                    "generated_at": self._now().isoformat(),
                }
                self._plans()[key] = plan
                attempts[key] = {"status": "complete", "at": self._now().isoformat()}
                self._save()
                return {"status": "generated", "plan": plan,
                        "wallet_bypass_used": wallet_bypass_used}
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                attempts[key] = {"status": "failed", "at": self._now().isoformat(), "error": self.last_error}
                self._save()
                return {"status": "failed", "reason": self.last_error,
                        "wallet_bypass_used": wallet_bypass_used}

    async def refresh(self, force=False, now=None):
        """Rebuild delivery rows from persisted timelines without invoking an LLM."""
        changed = False
        for plan in self._plans().values():
            derived = self._derive_deliveries(plan.get("timeline", []), plan.get("deliveries", []))
            if derived != plan.get("deliveries"):
                plan["deliveries"] = derived
                changed = True
        if changed:
            self._save()
        return changed

    def plan_for_start(self, start_at):
        return self._plans().get(self._plan_key(start_at))

    def current_plan(self, now=None):
        return self.plan_for_start(self.life_day(now)["start"])

    def next_plan(self, now=None):
        return self.plan_for_start(self.next_life_day(now)["start"])

    async def regenerate(self, which="current", now=None, *, allow_wallet_bypass=False):
        current = self.life_day(now)
        if which == "current":
            starts = [current["start"]]
        elif which == "next":
            starts = [current["end"]]
        elif which == "cycle":
            starts = [current["start"], current["end"]]
        else:
            return [{"status": "invalid", "reason": "expected current|next|cycle"}]
        return [await self.generate_life_day(
            start, force=True, allow_wallet_bypass=allow_wallet_bypass) for start in starts]

    def reset(self, which="current", now=None):
        current = self.life_day(now)
        starts = [current["start"]] if which == "current" else [current["end"]] if which == "next" else []
        removed = False
        for start in starts:
            key = self._plan_key(start)
            removed = self._plans().pop(key, None) is not None or removed
            self.state.setdefault("generation_attempts", {}).pop(key, None)
        if removed:
            self._save()
        return removed

    async def raw_cycle(self, now=None):
        window = self.life_day(now)
        plan = self.plan_for_start(window["start"])
        return {"life_day": window, "plan": plan,
                "timeline": plan.get("timeline", []) if plan else []}

    async def simulate_time(self, hhmm, now=None):
        current = self._now(now)
        if not isinstance(hhmm, str) or not re.fullmatch(r"\d{2}:\d{2}", hhmm):
            return None, "时间格式应为 HH:MM。"
        try:
            clock = time.fromisoformat(hhmm)
        except ValueError:
            return None, "无效的模拟时间。"
        window = self.life_day(current)
        start = window["end"].date() if clock < time(window["hour"], window["minute"]) else window["start"].date()
        simulated = datetime.combine(start, clock, tzinfo=window["start"].tzinfo)
        plan = self.plan_for_start(window["start"])
        if not plan:
            return None, "当前 life day 没有已生成计划。"
        isolated = copy.deepcopy(plan)
        result = await self.send_due(simulated, plans=[isolated], force_send=True)
        result["simulated_at"] = simulated.isoformat()
        if not result["hit_event_ids"] and not result["expired_event_ids"]:
            result["reason"] = "该时间没有到期事件。"
        elif not result["hit_event_ids"] and result["expired_event_ids"]:
            result["reason"] = "事件已超过宽限期，未发送。"
        elif not result["target_count"]:
            result["reason"] = "没有符合投递配置的目标。"
        elif result["failures"]:
            result["reason"] = "部分目标发送失败；详见失败明细。"
        return result, ""

    async def test_entry(self, entry_id, umo):
        event = next((entry for plan in self._plans().values()
                      for entry in plan.get("deliveries", [])
                      if entry.get("id") == entry_id), None)
        if not event or not event.get("message"):
            return False, "未找到可测试的 entry_id。"
        try:
            from astrbot.api.event import MessageChain
            result = await self.context.send_message(umo, MessageChain().message(event["message"]))
            if result is False:
                return False, f"测试操作失败：AstrBot 未确认发送 {entry_id}。"
            return True, f"测试操作：已向当前会话发送 {entry_id}。"
        except Exception as exc:
            return False, f"测试操作失败：{exc or type(exc).__name__}"

    async def send_due(self, now=None, *, plans=None, force_send=False):
        persist = plans is None
        target_umos = await self.targets()
        now = self._now(now)
        active = self._plans().values() if persist else plans
        result = {"hit_event_ids": [], "expired_event_ids": [], "target_count": len(target_umos),
                  "success_count": 0, "failures": []}
        if target_umos:
            from astrbot.api.event import MessageChain
        for plan in active:
            zone = _zone(plan.get("timezone", "Asia/Shanghai"))
            local_now = now.astimezone(zone)
            for event in plan.get("deliveries", []):
                if event.get("sent") or event.get("expired") or not event.get("message"):
                    continue
                trigger = _absolute(event.get("trigger_at", ""))
                if not trigger:
                    event["expired"] = True
                    result["expired_event_ids"].append(event.get("id", ""))
                    if persist:
                        self._save()
                    continue
                if local_now > trigger + timedelta(seconds=self._int("grace_seconds", 60)):
                    event["expired"] = True
                    result["expired_event_ids"].append(event.get("id", ""))
                    if persist:
                        self._save()
                    continue
                if local_now < trigger:
                    continue
                result["hit_event_ids"].append(event.get("id", ""))
                if not target_umos:
                    continue
                if self._get("dry_run", False) and not force_send:
                    if not event.get("dry_run_logged"):
                        logger.info("Life-day broadcast dry-run %s: %s", event["id"], event["message"])
                        event["dry_run_logged"] = True
                    if persist:
                        self._save()
                    continue
                delivered = set(event.get("delivered_umos", []))
                occurrences = {}
                for umo in target_umos:
                    n = occurrences.get(umo, 0)
                    occurrences[umo] = n + 1
                    states = self._target_delivery.get(umo, [])
                    target_state = states[n] if n < len(states) else {}
                    if self._was_delivered(delivered, umo, target_state):
                        continue
                    try:
                        sent = await self.context.send_message(umo, MessageChain().message(event["message"]))
                        if sent is False:
                            result["failures"].append({"entry_id": event.get("id", ""), "umo": umo,
                                                       "reason": "send_message returned False"})
                            continue
                        result["success_count"] += 1
                        delivered.add(target_state.get("key", umo))
                        event["delivered_umos"] = sorted(delivered)
                        if persist:
                            self._save()
                    except Exception as exc:
                        result["failures"].append({"entry_id": event.get("id", ""), "umo": umo,
                                                   "reason": str(exc) or type(exc).__name__})
                        logger.warning("Life-day broadcast send failed", exc_info=True)
                occurrences, all_delivered = {}, bool(target_umos)
                for umo in target_umos:
                    n = occurrences.get(umo, 0)
                    occurrences[umo] = n + 1
                    states = self._target_delivery.get(umo, [])
                    target_state = states[n] if n < len(states) else {}
                    if not self._was_delivered(delivered, umo, target_state):
                        all_delivered = False
                        break
                event["sent"] = all_delivered
                if persist:
                    self._save()
        result["failure_count"] = len(result["failures"])
        return result

    async def _maybe_generate(self, start_at):
        key = self._plan_key(start_at)
        if self.plan_for_start(start_at):
            return
        await self.generate_life_day(start_at)

    async def tick(self):
        now = self._now()
        window = self.life_day(now)
        await self._maybe_generate(window["start"])
        # A negative TimeAwareness generation clock means pre-generate the next
        # life day. This also catches up once if the service started before its
        # boundary and the prior day's scheduled pre-generation was missed.
        if window["target_day_offset"] > 0:
            await self._maybe_generate(window["end"])
        await self.refresh()
        await self.send_due(now)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Life-day planner tick failed", exc_info=True)
            await asyncio.sleep(self._int("poll_seconds", 15, 1))

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="xiaoman-life-day-planner")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def status(self, now=None):
        current, following = self.life_day(now), self.next_life_day(now)
        targets = await self.targets()
        provider = await self._provider(targets)
        current_input = await self.planner_input(current["start"], provider)
        wallet_policy = self.fat_fish.get_wallet_policy(at=self._now(now), provider_id=provider)
        pending = sorted((event for plan in self._plans().values() for event in plan.get("deliveries", [])
                          if event.get("message") and not event.get("sent") and not event.get("expired")),
                         key=lambda row: row.get("trigger_at", ""))
        return {
            "planner_version": PLANNER_VERSION,
            "boundary_clock": current["clock"],
            "current_life_day": current,
            "time_awareness_available": self.day_adapter.discover() is not None,
            "fat_fish_peaks": self.fat_fish.effective_peak_windows(
                current["start"], current["end"], provider,
                current_input["calendar_days"]),
            "protected_windows": current_input["protected_windows"],
            "free_windows": current_input["free_windows"],
            "peak_guard_before_minutes": self._int("peak_guard_before_minutes", 5),
            "peak_guard_after_minutes": self._int("peak_guard_after_minutes", 5),
            "admin_regenerate_bypass_fat_fish": bool(
                self._get("admin_regenerate_bypass_fat_fish", True)),
            "fat_fish_admins_bypass": bool(
                wallet_policy.get("found") and wallet_policy.get("admins_bypass", False)),
            "dry_run": bool(self._get("dry_run", False)),
            "current_plan_status": (self.current_plan(now) or {}).get("status", "missing"),
            "next_plan_status": (self.next_plan(now) or {}).get("status", "missing"),
            "next_broadcast": pending[0] if pending else None,
            "last_planner_error": self.last_error,
        }
