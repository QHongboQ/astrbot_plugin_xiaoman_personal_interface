"""Read-only adapter for the active TimeAwareness runtime services."""
from __future__ import annotations

import inspect
import re
from datetime import datetime
from astrbot.api import logger

TIME_AWARENESS_NAME = "time_awareness"


class TimeAwarenessAdapter:
    def __init__(self, context):
        self.context = context
        self.last_schedule: dict | None = None

    def discover(self):
        try:
            for metadata in self.context.get_all_stars():
                if getattr(metadata, "name", None) == TIME_AWARENESS_NAME:
                    if getattr(metadata, "activated", False) is True:
                        return getattr(metadata, "star_cls", None)
        except Exception:
            logger.warning("TimeAwareness schedule read failed", exc_info=True)
            return None
        return None

    def version(self):
        try:
            return next((str(getattr(meta, "version", "") or "") for meta in self.context.get_all_stars()
                         if getattr(meta, "name", None) == TIME_AWARENESS_NAME
                         and getattr(meta, "activated", False) is True), "")
        except Exception:
            return ""

    @staticmethod
    def _now(plugin, at=None):
        if at is not None:
            return at
        return plugin.time_context.now()

    def get_day_policy(self, at: datetime | None = None) -> dict:
        plugin = self.discover()
        result = {"available": False, "kind": "unknown", "label": "", "evaluated_at": at}
        if plugin is None:
            return result
        try:
            now = self._now(plugin, at)
            facts = plugin.time_context.facts.collect(scope="dashboard", now=now)
            workday = facts.workday
            kind = str(getattr(workday, "kind", "") or "unknown")
            available = bool(getattr(workday, "available", False)) and kind != "unknown"
            return {"available": available, "kind": kind if available else "unknown",
                    "label": str(getattr(workday, "value", "") or ""), "evaluated_at": facts.now}
        except Exception:
            return result

    async def get_daily_schedule(self, session: str, *, at: datetime | None = None,
                                 allow_generate: bool = False) -> dict | None:
        # A broadcaster may register identity but must never queue generation.
        if allow_generate:
            return None
        plugin = self.discover()
        self.last_schedule = None
        if plugin is None or not session:
            return None
        try:
            service = plugin.daily_schedule_service
            now = self._now(plugin, at)
            persona_hash = await service.register_session_async(session, trigger=False)
            if not persona_hash:
                return self._static_schedule(plugin, service, session, now)
            snapshot = service.get_snapshot_for_session(session, now=now)
            if not isinstance(snapshot, dict):
                return self._static_schedule(plugin, service, session, now)
            detail_fn = getattr(getattr(plugin, "daily_schedule_admin", None), "get_detail", None)
            if not callable(detail_fn):
                return None
            detail = detail_fn(persona_hash, now.date(), str(snapshot.get("timezone", "")), now=now)
            if inspect.isawaitable(detail):
                detail = await detail
            if not isinstance(detail, dict) or not isinstance(detail.get("slots"), list):
                return None
            result = {key: snapshot.get(key, detail.get(key, "")) for key in
                      ("persona_hash", "snapshot_id", "local_date", "timezone", "generated_at", "manually_edited")}
            result["source"] = "time_awareness"
            result["slots"] = [{key: slot.get(key, "") for key in
                                ("slot_ref", "start", "end", "name", "state", "origin", "source_origin")}
                               for slot in detail["slots"] if isinstance(slot, dict)]
            self.last_schedule = result
            return result
        except Exception:
            logger.warning("TimeAwareness schedule read failed", exc_info=True)
            return None

    def _static_schedule(self, plugin, service, session, now):
        """Use TimeAwareness' own effective summary API; never inspect its config."""
        summary_fn = getattr(service, "today_schedule_summary", None)
        if not callable(summary_fn):
            return None
        try:
            summary = summary_fn(session, now=now)
            if not isinstance(summary, str) or not summary.strip():
                return None
            slots = []
            for index, part in enumerate(summary.split("|"), 1):
                match = re.fullmatch(r"\s*(\d{1,2}:\d{2})-(\d{1,2}:\d{2})(?:\s+(.+?))?\s*", part)
                if not match:
                    continue
                slots.append({"slot_ref": f"static-{index}", "start": match.group(1),
                              "end": match.group(2), "name": match.group(3) or "",
                              "state": "", "origin": "static", "source_origin": "static"})
            if not slots:
                return None
            zone = getattr(now, "tzinfo", None)
            timezone = str(zone) if zone is not None else ""
            return {"source": "time_awareness", "persona_hash": "", "snapshot_id": "",
                    "local_date": now.date().isoformat(), "timezone": timezone,
                    "generated_at": "", "manually_edited": False, "slots": slots}
        except Exception:
            return None
