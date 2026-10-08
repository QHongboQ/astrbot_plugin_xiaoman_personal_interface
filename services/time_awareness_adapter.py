"""Read-only planner context from the active TimeAwareness runtime."""
from __future__ import annotations

from datetime import datetime, timedelta, time
import inspect

TIME_AWARENESS_NAME = "time_awareness"


def _mapping(value):
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        pass
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        try:
            result = converter()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    getter = getattr(value, "get", None)
    if callable(getter):
        result = {}
        for key in ("daily_schedule", "ai_daily", "adaptive", "weather_sensor", "random",
                    "generation_time", "worldview", "use_persona", "theme_pool", "style_pool",
                    "allow_custom_theme", "recent_days", "state_continuity_enabled", "enabled",
                    "random_enabled", "random_weather_enabled", "random_weather"):
            try:
                item = getter(key, None)
                if item is not None:
                    result[key] = item
            except Exception:
                continue
        return result
    return {}


def _pool(value):
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class TimeAwarenessAdapter:
    """Read calendar, clock, worldview and sensor data without invoking generation."""

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
        try:
            now = plugin.time_context.now() if plugin else None
            if isinstance(now, datetime):
                return now
        except Exception:
            pass
        return datetime.now().astimezone()

    @staticmethod
    def _config(plugin):
        return _mapping(getattr(plugin, "config", None))

    def get_generation_boundary(self) -> dict:
        plugin = self.discover()
        config = self._config(plugin) if plugin else {}
        daily = _mapping(config.get("daily_schedule"))
        ai_daily = _mapping(daily.get("ai_daily"))
        raw = str(ai_daily.get("generation_time", "-04:00") or "-04:00").strip()
        target_offset = 1 if raw.startswith("-") else 0
        clock = raw[1:].strip() if raw[:1] in {"-", "+"} else raw
        try:
            parsed = time.fromisoformat(clock)
        except ValueError:
            return {"available": bool(plugin), "raw": raw, "clock": "04:00",
                    "hour": 4, "minute": 0, "target_day_offset": target_offset}
        return {"available": bool(plugin), "raw": raw,
                "clock": f"{parsed.hour:02d}:{parsed.minute:02d}",
                "hour": parsed.hour, "minute": parsed.minute,
                "target_day_offset": target_offset}

    def life_day_window(self, at: datetime | None = None) -> dict:
        now = at or self.current_time()
        now = now if now.tzinfo else now.astimezone()
        boundary = self.get_generation_boundary()
        start = now.replace(hour=boundary["hour"], minute=boundary["minute"], second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        return {**boundary, "start": start, "end": start + timedelta(days=1),
                "timezone": str(getattr(getattr(self.discover(), "time_context", None), "timezone", "")
                                 or now.tzinfo)}

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

    async def planner_context(self, start_at: datetime, end_at: datetime) -> dict:
        plugin = self.discover()
        if plugin is None:
            return {"available": False, "calendar_days": [], "worldview": "", "persona": "",
                    "theme_pool": [], "style_pool": [], "allow_custom_theme": True,
                    "adaptive": {}, "weather": []}
        config = self._config(plugin)
        daily = _mapping(config.get("daily_schedule"))
        ai_daily = _mapping(daily.get("ai_daily"))
        adaptive = _mapping(ai_daily.get("adaptive"))
        calendar_days = []
        day = start_at.date()
        last = (end_at - timedelta(microseconds=1)).date()
        while day <= last:
            at = datetime.combine(day, time(12), tzinfo=start_at.tzinfo)
            policy = self.get_day_policy(at)
            calendar_days.append({"date": day.isoformat(), "kind": policy.get("kind", "unknown"),
                                  "label": policy.get("label", "")})
            day += timedelta(days=1)

        weather = []
        weather_cfg = _mapping(config.get("weather_sensor"))
        random_cfg = _mapping(weather_cfg.get("random"))
        weather_enabled = bool(ai_daily.get("random_weather_enabled", random_cfg.get(
            "enabled", weather_cfg.get("random_enabled", weather_cfg.get(
                "random_weather_enabled", weather_cfg.get("random_weather", False))))))
        sensor = getattr(plugin, "weather_sensor", None)
        forecast = getattr(sensor, "daily_forecast", None)
        if weather_enabled and callable(forecast):
            for row in calendar_days:
                try:
                    value = forecast(datetime.fromisoformat(row["date"]).date())
                    if inspect.isawaitable(value):
                        value = await value
                    weather.append({"date": row["date"], "forecast": value})
                except Exception:
                    continue

        return {
            "available": True,
            "worldview": str(ai_daily.get("worldview", "") or ""),
            "use_persona": bool(ai_daily.get("use_persona", True)),
            "theme_pool": _pool(adaptive.get("theme_pool", [])),
            "style_pool": _pool(adaptive.get("style_pool", [])),
            "allow_custom_theme": bool(adaptive.get("allow_custom_theme", True)),
            "adaptive": {key: adaptive.get(key) for key in
                         ("recent_days", "state_continuity_enabled")},
            "calendar_days": calendar_days,
            "weather": weather,
        }
