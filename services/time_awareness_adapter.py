"""Read-only adapter for active TimeAwareness v2.3.0 runtime APIs."""
from __future__ import annotations

from datetime import datetime, timedelta

TIME_AWARENESS_NAME = "time_awareness"


class TimeAwarenessAdapter:
    def __init__(self, context):
        self.context = context

    def discover(self):
        try:
            return next((star.star_cls for star in self.context.get_all_stars()
                         if star.name == TIME_AWARENESS_NAME and star.activated is True), None)
        except Exception:
            return None

    def current_time(self) -> datetime:
        plugin = self.discover()
        if plugin is not None:
            try:
                now = plugin.time_context.now()
                if isinstance(now, datetime):
                    return now
            except Exception:
                pass
        return datetime.now().astimezone()

    def get_generation_boundary(self) -> dict:
        """Read TimeAwareness ai_daily.generation_time without mutating its config."""
        plugin = self.discover()
        if plugin is None:
            return {
                "available": False,
                "raw": "",
                "hour": 0,
                "minute": 5,
                "clock": "00:05",
                "target_day_offset": 0,
            }
        try:
            service = plugin.daily_schedule_service
            getter = getattr(service, "_daily_config", None)
            config = getter() if callable(getter) else {}
            if not isinstance(config, dict):
                config = {}
            raw = str(config.get("generation_time", "00:05") or "00:05").strip()
            parser = getattr(service, "_parse_generation_time", None)
            if callable(parser):
                hour, minute, target_day_offset = parser(raw)
            else:
                normalized = raw[1:].strip() if raw.startswith("-") else raw
                parsed = datetime.strptime(normalized, "%H:%M")
                hour, minute = parsed.hour, parsed.minute
                target_day_offset = 1 if raw.startswith("-") else 0
            return {
                "available": True,
                "raw": raw,
                "hour": int(hour),
                "minute": int(minute),
                "clock": f"{int(hour):02d}:{int(minute):02d}",
                "target_day_offset": int(target_day_offset),
            }
        except Exception:
            return {
                "available": False,
                "raw": "",
                "hour": 0,
                "minute": 5,
                "clock": "00:05",
                "target_day_offset": 0,
            }

    def rolling_day_window(self, at: datetime | None = None) -> dict:
        """Return Xiaoman's 24h life-day window using TimeAwareness's configured clock."""
        now = at or self.current_time()
        if not isinstance(now, datetime):
            now = self.current_time()
        if now.tzinfo is None:
            now = now.astimezone()
        boundary = self.get_generation_boundary()
        start = now.replace(
            hour=int(boundary["hour"]),
            minute=int(boundary["minute"]),
            second=0,
            microsecond=0,
        )
        if now < start:
            start -= timedelta(days=1)
        return {
            **boundary,
            "start": start,
            "end": start + timedelta(days=1),
        }

    def get_day_policy(self, at: datetime | None = None) -> dict:
        plugin = self.discover()
        if plugin is None:
            return {"available": False, "kind": "unknown", "label": "", "evaluated_at": at}
        try:
            now = at or plugin.time_context.now()
            facts = plugin.time_context.facts.collect(scope="dashboard", now=now)
            workday = facts.workday
            kind = str(workday.kind or "unknown")
            return {"available": bool(workday.available) and kind != "unknown",
                    "kind": kind if workday.available else "unknown",
                    "label": str(workday.value or ""), "evaluated_at": facts.now}
        except Exception:
            return {"available": False, "kind": "unknown", "label": "", "evaluated_at": at}

    async def get_daily_schedule(self, session: str, *, at: datetime | None = None,
                                 allow_generate: bool = False) -> dict | None:
        if allow_generate or not session:
            return None
        plugin = self.discover()
        if plugin is None:
            return None
        try:
            service = plugin.daily_schedule_service
            now = at or plugin.time_context.now()
            persona_hash = await service.register_session_async(session, trigger=False)
            if not persona_hash:
                return None
            snapshot = service.get_snapshot_for_session(session, now=now)
            if not isinstance(snapshot, dict):
                return None
            detail = plugin.daily_schedule_admin.get_detail(
                persona_hash, now.date(), str(snapshot.get("timezone", "")), now=now)
            if not isinstance(detail, dict) or not isinstance(detail.get("slots"), list):
                return None
            result = {key: snapshot.get(key, detail.get(key, "")) for key in
                      ("persona_hash", "snapshot_id", "local_date", "timezone", "generated_at", "manually_edited")}
            result["source"] = "time_awareness"
            result["slots"] = [{key: slot.get(key, "") for key in
                                ("slot_ref", "start", "end", "name", "state", "origin", "source_origin")}
                               for slot in detail["slots"] if isinstance(slot, dict)]
            return result
        except Exception:
            return None
