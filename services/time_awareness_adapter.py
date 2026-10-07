"""Read-only adapter for active TimeAwareness v2.3.0 runtime APIs."""
from __future__ import annotations

from datetime import datetime

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
