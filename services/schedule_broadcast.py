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

PLANNER_VERSION = "0.8.5"
SCHEMA_VERSION = 2
DAILY_THEME_MAX_CHARS = 60
DAILY_STYLE_MAX_CHARS = 40
ENTRY_NAME_MAX_CHARS = 48
NORMAL_STATE_MAX_CHARS = 120
BRIDGE_STATE_MAX_CHARS = 240
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
            final = categorized[-1] if categorized else (None, "other")
            final_category = final[1] if final[0] else None
            completed.append({"life_day_start": plan.get("life_day_start"),
                              "daily_theme": plan.get("daily_theme", ""),
                              "daily_style": plan.get("daily_style", ""),
                              "major_activities": names,
                              "categories_used": sorted({category for _row, category in categorized}),
                              "previous_tail": tail,
                              "previous_final_category": final_category,
                              "previous_life_day_ended_awake": (
                                  False if final_category == "sleep"
                                  else True if final_category and final_category != "mixed"
                                  else None),
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
        continuity = {
            "has_previous_life_day": bool(latest),
            "previous_tail": latest.get("previous_tail", []) if latest else [],
            "previous_final_category": latest.get("previous_final_category") if latest else None,
            "previous_life_day_ended_awake": (
                latest.get("previous_life_day_ended_awake") if latest else None),
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
            "先在内部安排并核对完整 timeline，再为同一份最终 timeline 选择 daily_theme 和 daily_style；主题/风格必须忠实概括实际安排，不得与任一时段矛盾，也不得把一天中的一个小阶段夸大成整天。只有时间线确实支持时才可使用‘整天’、‘半天’、‘睡到中午’、‘通宵’、‘全天宅家’等说法。"
            "林小满是课表相对宽松的艺术专业大学生；protected_window 只是结构性规划窗口，不代表上课时间或课程安排。BRIDGE 必须是该保护窗内的一个大活动，但可以是工作室创作、外出、购物、休息、睡觉、社交、娱乐、旅行或有依据的课程；上课只是选项，不得仅因小满是大学生就默认课堂/食堂/自习/宿舍是每日主轴，也不得把两个 protected_window 模板化地都安排成课。若两个保护窗都是课，应有世界观、日历、历史或其他上下文依据/自然主题关联；否则优先考虑仅一段课程、无课、工作室/项目、临时外出、恢复休息或社交休闲。近期历史若连续多天偏上课，可在上下文允许时换一种结构；不硬性禁止上课，也不为求新奇而违背强上下文。"
            "activity_pool 是小满对具体活动的加权偏好，权重只表示相对偏好，不是精确概率；现实、天气、精力、睡眠和近期重复式样优先于权重。"
            "activity_pool_allow_custom=true 时可自然安排池外活动；为 false 时，主要休闲/社交活动应来自活动池，除非世界观、日历或既有连续性要求其他安排。不得重复实现 theme_pool：主题池决定日子是什么感觉，活动池提供具体可做的事。"
            "activity_density 控制的是主要活动强度/数量，不是每个小动作或时间转换的事件数：relaxed 约1-2项主要活动并留大量自由/休息时间；balanced 约2-3项；busy 约3-4项。主要活动例如课程、工作室创作、电玩城、密室、KTV、购物出行或社交聚会；通勤、饭前吃饭、换衣、买咖啡、洗澡、刷手机、走去附近店铺通常只是支持动作，不应各自算成主要活动。不得为凑数量制造活动。完整覆盖24小时不代表必须保持忙碌。睡觉、躺着、打游戏、看视频、发呆、休息、聊天、通勤和慢慢吃饭都可以是长 NORMAL 区块。"
            "正常生命日通常应包含一些睡眠或休息，避免24小时连续高强度活动；睡眠只是普通生活事件，睡多久、何时醒来由规划器结合整日主题和上下文自由安排。不要计算或补偿前一日睡眠时长，不要推导睡眠债、恢复时长或必须起床时间，也不要因为晚睡就推断必须晚起，更不要为了证明早起合理而编造早课或截止日期。不要因为上一生命日曾在睡觉就强制新生命日继续睡。"
            "protected_windows 是结构性的规划锚点，优先级高于睡眠历史；睡眠历史绝不能占用、改变或使保护窗失效。每个保护窗一个 BRIDGE；保护窗不是课程表，绝不默认 08:55-12:05 是上午课或 13:55-18:05 是下午课。BRIDGE 可按整日主题与上下文安排上课、游乐园、工作室创作、购物、密室、慢启动、休息、睡觉或混合活动；类别和内容必须有上下文支持。不要反复套用‘上午上课→午饭→下午上课→晚上娱乐’模板。允许一天只有一节课、没有课、工作室/项目、临时外出、恢复休息或社交休闲；若近期生命日已连续多天以课程为主，历史应促使你在合理范围内考虑不同结构，但不硬禁课程、不忽略真实上下文。不存在睡眠推导出的起床时间。"
            "在 free_window 内，较长睡眠通常应作为独立 category=sleep 的 NORMAL 事件，不要把补觉/回笼觉藏在其他事件的 state。protected_window 的 BRIDGE 必须保持一个不可拆分的顶层事件：若它跨越自然的睡眠→醒来→慢启动阶段，可在同一 state 中描述内部过程，通常使用 category=mixed；不得为了避免 mixed 而提前叫醒小满或把 BRIDGE 拆开。若整个 BRIDGE 确实都在睡觉，category=sleep 仍然合适。睡眠/休息是有效的低强度时段，无需替换为活动。"
            "只根据 continuity 和 recent_life_days 中明确提供的历史延续。若无可用历史，必须视为没有已知的前夜/前几日事件；不得编造‘昨晚通宵赶作业’等事实，只生成合理的起始状态。近期主要活动、类别、主题、风格和睡眠只用于避免重复及维持连续性。"
            "优先遵循 worldview、theme_pool、style_pool、天气和真实日历。不要每天塞满高强度活动。人物、地点、天气影响、消费和结果不得无依据编造。"
            "只输出紧凑 JSON，不要 Markdown、代码围栏、解释或 JSON 前后的文字，尽量避免无意义空白和冗长叙述。字段：daily_theme,daily_style,timeline。daily_theme 不超过60字符，daily_style 不超过40字符。时间为带时区 ISO 8601。"
            "timeline 每项都必须包含 category，且只能是 sleep,rest,meal,travel,school,creative,social,entertainment,outdoor,shopping,errand,mixed,other 之一。"
            "每项 name 不超过48字符。NORMAL 项字段：id,kind=NORMAL,category,start_at,end_at,name,state,broadcast_message。NORMAL state 不超过120字符，通常只写1句简洁连续性信息。"
            "BRIDGE 项字段：id,kind=BRIDGE,category,start_at,end_at,name,state,source_peak_start,source_peak_end,enter_message,exit_message；source_peak_* 原样照抄对应窗口。BRIDGE state 不超过240字符，可写内部阶段，通常用2-4句或短阶段描述。"
            "state 只保留后续规划需要的信息，不写小说式叙述；播报消息仍须自然、有个性。"
            "NORMAL 顶层事件代表有意义的生活阶段，而不是每个身体动作。只有主要目的、核心活动、社交对象/情境、地点/外出阶段、精力/状态阶段或睡眠休息阶段发生有意义变化时，才通常值得另开一项。相同目的、同一外出/地点链或连续过渡中的小动作应合并到一个 NORMAL 的 name/state；例如吃饭+买咖啡、回家换衣+吃饭再出门、打车回家+洗澡+躺床刷手机通常合并。买咖啡、换衣、打车、洗脸、洗澡、看手机、走到附近另一家店通常不单独成项，除非它本身构成有意义的独立阶段。不要过度合并不同的主要活动，例如电玩城和夜市宵夜可以分别成项。"
            "事件数量是软指导而非硬指标：balanced 时约2小时以内的短 free_window 通常1个 NORMAL；约2-5小时通常1-2个；约5-10小时通常2-4个。像18:05到次日04:00这样的长晚间 free_window，balanced 通常约3-5个顶层 NORMAL。relaxed 使用更少、更长的区块；busy 可稍多。不要为了命中数量而机械拆分，不得仅因数量拒绝或改写有效安排。"
            "daily_theme / daily_style 必须概括本次返回的最终 timeline：先确定时间线，再选主题和风格；不得与实际时间线冲突，不要把一个阶段夸大成全天。除非时间线确实支持，不要声称‘睡到中午’、‘摆烂半天’、‘整天宅家’、‘通宵’或‘全天宅家’；例如时间线有早晚课程，就不能称‘摆烂半天’，有早间活动不能称‘睡到中午’，晚上外出不能称‘整天宅家’。"
            f"每条消息不得超过 {self._int('max_message_chars', 80, 1)} 个字符。"
        )
        extra = str(self._get("planner_prompt", "") or "").strip()
        return guidance + (f"\n补充规划要求：{extra}" if extra else "") + "\nPLANNER_INPUT:\n" + json.dumps(planner_input, ensure_ascii=False, default=str)

    @staticmethod
    def _normalize_planner_json(raw):
        """Strip whitespace and at most one complete outer Markdown fence."""
        text = str(raw or "").strip()
        if not (text.startswith("```") and text.endswith("```")):
            return text
        body = text[3:-3].strip()
        if body[:4].lower() == "json" and (len(body) == 4 or body[4].isspace()):
            body = body[4:].strip()
        return body

    @staticmethod
    def _json_error_snippet(raw, start, end, limit=160):
        text = str(raw or "")
        start = max(0, min(len(text), start))
        end = max(start, min(len(text), end))
        if end - start > limit:
            end = start + limit
        return text[start:end].replace("\r", " ").replace("\n", " ")

    @classmethod
    def _log_planner_json_error(cls, raw, exc, response):
        raw = str(raw or "")
        near_start = max(0, exc.pos - 80)
        near_end = min(len(raw), exc.pos + 80)
        near_error = cls._json_error_snippet(raw, near_start, near_end)
        raw_tail = cls._json_error_snippet(raw, max(0, len(raw) - 240), len(raw), 240)
        usage = getattr(response, "usage", None)
        output_tokens = getattr(usage, "output", None) if usage is not None else None
        tokens = f" output_tokens={output_tokens}" if isinstance(output_tokens, int) else ""
        logger.warning(
            "planner JSON parse failed raw_length={} error_line={} error_column={} "
            "error_pos={}{} near_error={!r} raw_tail={!r}",
            len(raw), exc.lineno, exc.colno, exc.pos, tokens, near_error, raw_tail,
        )

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
            name = str(row.get("name", "")).strip()
            if not name:
                return False, f"{ident} missing name"
            if len(name) > ENTRY_NAME_MAX_CHARS:
                return False, f"{ident} name exceeds {ENTRY_NAME_MAX_CHARS} characters"
            if not isinstance(row.get("state"), str):
                return False, f"{ident} missing state"
            state_limit = NORMAL_STATE_MAX_CHARS if row["kind"] == "NORMAL" else BRIDGE_STATE_MAX_CHARS
            if len(row["state"]) > state_limit:
                return False, f"{ident} state exceeds {state_limit} characters"
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
    def validate_daily_metadata(result):
        theme = str(result.get("daily_theme", "") or "").strip()
        style = str(result.get("daily_style", "") or "").strip()
        if not theme or not style:
            return False, "planner response missing daily_theme or daily_style"
        if len(theme) > DAILY_THEME_MAX_CHARS:
            return False, f"daily_theme exceeds {DAILY_THEME_MAX_CHARS} characters"
        if len(style) > DAILY_STYLE_MAX_CHARS:
            return False, f"daily_style exceeds {DAILY_STYLE_MAX_CHARS} characters"
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
                normalized = self._normalize_planner_json(raw)
                try:
                    result = json.loads(normalized)
                except json.JSONDecodeError as exc:
                    self._log_planner_json_error(normalized, exc, response)
                    raise ValueError(
                        f"planner returned invalid JSON: {exc.msg} at line {exc.lineno} "
                        f"column {exc.colno} position {exc.pos}"
                    ) from exc
                if not isinstance(result, dict):
                    raise ValueError("planner response must be a JSON object")
                metadata_valid, metadata_reason = self.validate_daily_metadata(result)
                if not metadata_valid:
                    raise ValueError("planner output rejected: " + metadata_reason)
                timeline = result.get("timeline")
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
