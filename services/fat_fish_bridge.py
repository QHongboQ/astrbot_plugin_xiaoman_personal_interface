"""Read-only Fat Fish policy and peak-period adapter."""
from __future__ import annotations

from datetime import datetime, timedelta, time
from importlib import import_module
from zoneinfo import ZoneInfo

FAT_FISH_NAME = "astrbot_plugin_fat_fish_wallet"


class FatFishBridge:
    """Consume the public wallet policy without patching Fat Fish runtime state."""

    def __init__(self, context, day_adapter):
        self.context = context
        self.day_adapter = day_adapter

    def discover(self):
        try:
            return next((star.star_cls for star in self.context.get_all_stars()
                         if star.name == FAT_FISH_NAME and star.activated is True), None)
        except Exception:
            return None

    def get_wallet_policy(self, *, at=None, provider_id=None):
        fish = self.discover()
        getter = getattr(fish, "get_wallet_policy", None)
        if callable(getter):
            try:
                return {"found": True, **getter(at=at, provider_id=provider_id)}
            except Exception:
                pass
        return {"found": False, "enabled": False, "allowed": False,
                "state": "missing", "manual_override": "auto", "provider_affected": False}

    def effective_peak_windows(self, life_day_start, life_day_end, provider_id="", calendar_days=None):
        """Return effective configured peak intervals intersecting a life day.

        TimeAwareness's adjusted-workday classification is authoritative for the
        date type. Fat Fish's public policy supplies enabled/override/provider and
        peak settings; the official scheduler functions apply its weekday/period rules.
        """
        fish = self.discover()
        getter = getattr(fish, "get_wallet_policy", None)
        if not callable(getter):
            return []
        try:
            initial_policy = getter(at=life_day_start, provider_id=provider_id)
        except Exception:
            initial_policy = {}
        timezone_name = str(initial_policy.get("timezone") or
                            getattr(life_day_start.tzinfo, "key", None) or life_day_start.tzinfo)
        try:
            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = life_day_start.tzinfo
            timezone_name = str(zone)
        start = life_day_start.astimezone(zone)
        end = life_day_end.astimezone(zone)
        kinds = {row["date"]: row.get("kind", "unknown") for row in (calendar_days or [])}
        scheduler = import_module(fish.__class__.__module__.rsplit(".", 1)[0] + ".scheduler")
        windows = []
        natural_day = start.date()
        last_day = (end - timedelta(microseconds=1)).date()
        while natural_day <= last_day:
            kind = kinds.get(natural_day.isoformat(), "unknown")
            if kind not in {"workday", "adjusted", "adjusted_workday"}:
                natural_day += timedelta(days=1)
                continue
            noon = datetime.combine(natural_day, time(12), tzinfo=zone)
            try:
                policy = getter(at=noon, provider_id=provider_id)
            except Exception:
                policy = {}
            if (not policy.get("enabled")
                    or str(policy.get("manual_override", "auto")) != "auto"
                    or not policy.get("provider_affected")):
                natural_day += timedelta(days=1)
                continue
            periods = scheduler.parse_periods(str(policy.get("peak_periods", "") or ""))
            weekdays = scheduler.parse_weekdays(str(policy.get("peak_weekdays", "") or ""))
            if not weekdays or natural_day.weekday() in weekdays:
                midnight = datetime.combine(natural_day, time.min, tzinfo=zone)
                for period in periods:
                    peak_start = midnight + timedelta(seconds=period.start)
                    peak_end = midnight + timedelta(seconds=period.end)
                    peak_start = max(peak_start, start)
                    peak_end = min(peak_end, end)
                    if peak_start < peak_end:
                        windows.append({"start_at": peak_start, "end_at": peak_end,
                                        "source_peak_start": midnight + timedelta(seconds=period.start),
                                        "source_peak_end": midnight + timedelta(seconds=period.end)})
            natural_day += timedelta(days=1)
        return windows
