"""Read-only adapter for the Fat Fish Wallet v1.1.1 public config/runtime shape."""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

FAT_FISH_NAME = "astrbot_plugin_fat_fish_wallet"
WORKDAY_KINDS = {"workday", "adjusted", "adjusted_workday"}


def _config_value(config, key, default=None):
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                value = getter(key)
                return default if value is None else value
            except Exception:
                pass
        except Exception:
            pass
    return default


def _parse_periods(spec):
    """Small equivalent of Fat Fish scheduler.parse_periods()."""
    result = []
    for part in str(spec or "").split(","):
        if "-" not in part:
            continue
        try:
            left, right = part.strip().split("-", 1)
            sh, sm = (int(value) for value in left.strip().split(":", 1))
            eh, em = (int(value) for value in right.strip().split(":", 1))
            start, end = sh * 3600 + sm * 60, eh * 3600 + em * 60
            if end > start:
                result.append((start, end))
        except (ValueError, TypeError):
            continue
    return result


def _parse_weekdays(spec):
    days = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.append(int(part))
    return days


def _zone(name):
    try:
        return ZoneInfo(str(name))
    except Exception:
        value = str(name)
        if value in {"Asia/Shanghai", "UTC+08:00", "+08:00"}:
            return timezone(timedelta(hours=8), "Asia/Shanghai")
        if value in {"UTC", "Etc/UTC", "Z"}:
            return timezone.utc
        if value.startswith("UTC"):
            value = value[3:]
        if len(value) == 6 and value[0] in "+-" and value[3] == ":":
            try:
                hours, minutes = int(value[1:3]), int(value[4:6])
                offset = timedelta(hours=hours, minutes=minutes)
                return timezone(offset if value[0] == "+" else -offset)
            except ValueError:
                pass
        return timezone.utc


class FatFishBridge:
    """Adapt Fat Fish config without calling private methods or mutating state."""

    def __init__(self, context, day_adapter):
        self.context = context
        self.day_adapter = day_adapter

    def discover(self):
        try:
            return next((star.star_cls for star in self.context.get_all_stars()
                         if star.name == FAT_FISH_NAME and star.activated is True), None)
        except Exception:
            return None

    def _provider_matches(self, provider_id, terms, gate_unknown):
        terms = str(terms or "").strip()
        provider = None
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
        provider_resolved = provider is not None
        if not terms:
            return False, provider_resolved
        if terms == "*":
            return True, provider_resolved
        keywords = [item.strip().lower() for item in terms.split(",") if item.strip()]
        if not keywords:
            return False, provider_resolved
        if provider is None:
            return bool(gate_unknown), False
        try:
            meta = provider.meta()
            haystack = " ".join(str(getattr(meta, key, "") or "")
                                for key in ("id", "model", "type")).lower()
        except Exception:
            return bool(gate_unknown), False
        return any(term in haystack for term in keywords), provider_resolved

    def _policy(self, fish, at, provider_id, day_kind):
        if fish is None:
            return {"found": False, "enabled": False, "timezone": "Asia/Shanghai",
                    "manual_override": "auto", "provider_affected": False,
                    "peak_periods": "", "peak_weekdays": "", "state": "missing",
                    "allowed": True, "admins_bypass": True, "day_kind": day_kind}
        config = getattr(fish, "config", None)
        enabled = bool(_config_value(config, "enabled", True))
        timezone_name = str(_config_value(config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        zone = _zone(timezone_name)
        if zone is timezone.utc and timezone_name not in {"UTC", "Etc/UTC", "Z"}:
            timezone_name = "UTC"
        if at is None:
            at = datetime.now(zone)
        elif at.tzinfo is None:
            at = at.replace(tzinfo=zone)
        else:
            at = at.astimezone(zone)
        override = str(_config_value(config, "manual_override", "auto") or "auto")
        admins_bypass = bool(_config_value(config, "admins_bypass", True))
        peak_periods = str(_config_value(config, "peak_periods", "09:00-12:00,14:00-18:30") or "")
        peak_weekdays = str(_config_value(config, "peak_weekdays", "0,1,2,3,4,5,6") or "")
        provider_affected, provider_resolved = self._provider_matches(
            provider_id,
            _config_value(config, "affected_providers", "deepseek"),
            _config_value(config, "gate_when_provider_unknown", True),
        )
        periods = _parse_periods(peak_periods)
        weekdays = _parse_weekdays(peak_weekdays)
        weekday_active = not weekdays or at.weekday() in weekdays
        second = at.hour * 3600 + at.minute * 60 + at.second
        calendar_active = day_kind in WORKDAY_KINDS
        peak = calendar_active and weekday_active and any(start <= second < end for start, end in periods)

        if not enabled:
            state, allowed = "offpeak", True
        elif override == "always_allow":
            state, allowed = "forced allow", True
        elif override == "always_block":
            state, allowed = "forced block", False
        else:
            state = "peak" if peak else "offpeak"
            allowed = not provider_affected or not peak
        return {"found": True, "enabled": enabled, "timezone": timezone_name,
                "manual_override": override, "provider_affected": provider_affected,
                "peak_periods": peak_periods, "peak_weekdays": peak_weekdays,
                "admins_bypass": admins_bypass, "provider_resolved": provider_resolved,
                "state": state, "allowed": allowed, "day_kind": day_kind,
                "evaluated_at": at}

    def get_wallet_policy(self, *, at=None, provider_id=None):
        fish = self.discover()
        if fish is None:
            return self._policy(None, at, provider_id, "unknown")
        config = getattr(fish, "config", None)
        timezone_name = str(_config_value(config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        zone = _zone(timezone_name)
        local_at = (datetime.now(zone) if at is None else
                    at.replace(tzinfo=zone) if at.tzinfo is None else at.astimezone(zone))
        try:
            day_policy = self.day_adapter.get_day_policy(local_at)
            day_kind = str(day_policy.get("kind", "unknown")) if day_policy.get("available") else "unknown"
        except Exception:
            day_kind = "unknown"
        return self._policy(fish, local_at, provider_id, day_kind)

    def effective_peak_windows(self, life_day_start, life_day_end, provider_id="", calendar_days=None):
        """Return configured auto peaks that apply to workdays in this life day."""
        fish = self.discover()
        if fish is None:
            return []
        config = getattr(fish, "config", None)
        base = self._policy(fish, life_day_start, provider_id, "workday")
        if (not base["enabled"]
                or base["manual_override"] in {"always_allow", "always_block"}
                or not base["provider_affected"]):
            return []
        zone = _zone(base["timezone"])
        start, end = life_day_start.astimezone(zone), life_day_end.astimezone(zone)
        kinds = {str(row.get("date")): str(row.get("kind", "unknown"))
                 for row in (calendar_days or []) if isinstance(row, dict)}
        periods = _parse_periods(base["peak_periods"])
        weekdays = _parse_weekdays(base["peak_weekdays"])
        windows = []
        natural_day = start.date()
        last_day = (end - timedelta(microseconds=1)).date()
        while natural_day <= last_day:
            if kinds.get(natural_day.isoformat()) not in WORKDAY_KINDS:
                natural_day += timedelta(days=1)
                continue
            if not weekdays or natural_day.weekday() in weekdays:
                midnight = datetime.combine(natural_day, time.min, tzinfo=zone)
                for peak_start_seconds, peak_end_seconds in periods:
                    source_start = midnight + timedelta(seconds=peak_start_seconds)
                    source_end = midnight + timedelta(seconds=peak_end_seconds)
                    peak_start, peak_end = max(source_start, start), min(source_end, end)
                    if peak_start < peak_end:
                        windows.append({"start_at": peak_start, "end_at": peak_end,
                                        "source_peak_start": source_start,
                                        "source_peak_end": source_end})
            natural_day += timedelta(days=1)
        return windows
