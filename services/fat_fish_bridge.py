"""Small runtime bridge for Fat Fish 1.1.1 wallet policy."""
from __future__ import annotations

from datetime import datetime
from importlib import import_module
from zoneinfo import ZoneInfo

FAT_FISH_NAME = "astrbot_plugin_fat_fish_wallet"


class FatFishBridge:
    def __init__(self, context, day_adapter):
        self.context = context
        self.day_adapter = day_adapter
        self.instance = None
        self._original_cfg = None
        self._cfg_wrapper = None

    def discover(self):
        try:
            return next((star.star_cls for star in self.context.get_all_stars()
                         if star.name == FAT_FISH_NAME and star.activated is True), None)
        except Exception:
            return None

    def install(self):
        fish = self.discover()
        if fish is None:
            return False
        self.instance = fish
        if self._cfg_wrapper is not None and fish._cfg is self._cfg_wrapper:
            return True
        original = fish._cfg
        bridge = self

        def cfg_wrapper(key, *args, **kwargs):
            value = original(key, *args, **kwargs)
            if key != "manual_override" or value != "auto":
                return value
            day = bridge.day_adapter.get_day_policy()
            if day.get("available") and day.get("kind") in {"holiday", "weekend"}:
                return "always_allow"
            return value

        self._original_cfg = original
        self._cfg_wrapper = cfg_wrapper
        fish._cfg = cfg_wrapper
        return True

    def _config(self, key, default=None):
        try:
            return self._original_cfg(key, default)
        except Exception:
            return default

    def get_wallet_policy(self, *, at=None, provider_id=None):
        fish = self.discover()
        if fish is None:
            return {"found": False, "enabled": False, "allowed": False,
                    "state": "missing", "day_kind": "unknown"}
        if self.instance is not fish or self._original_cfg is None:
            self.install()
        self.instance = fish
        now = at or datetime.now().astimezone()
        day = self.day_adapter.get_day_policy(now)
        enabled = bool(self._config("enabled", self._config("enable", True)))
        override = str(self._config("manual_override", "auto") or "auto")
        timezone = str(self._config("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            local = now.replace(tzinfo=ZoneInfo(timezone)) if now.tzinfo is None else now.astimezone(ZoneInfo(timezone))
        except Exception:
            local = now

        affected = True
        matcher = getattr(fish, "_provider_affected", None)
        if callable(matcher):
            affected = bool(matcher(provider_id))

        if not enabled:
            allowed, state = True, "disabled"
        elif override == "always_allow":
            allowed, state = True, "forced_allow"
        elif override == "always_block":
            allowed, state = False, "forced_block"
        elif day.get("available") and day.get("kind") in {"holiday", "weekend"}:
            allowed, state = True, "offpeak"
        elif affected:
            periods, weekdays = fish._periods(), fish._weekdays()
            peak = getattr(fish, "_is_peak", None)
            if not callable(peak):
                module = import_module(fish.__class__.__module__.rsplit(".", 1)[0] + ".scheduler")
                peak = module.is_peak
            in_peak = peak(local, periods, weekdays)
            allowed, state = (False, "peak") if in_peak else (True, "offpeak")
        else:
            allowed, state = True, "offpeak"

        return {"found": True, "enabled": enabled, "allowed": allowed, "state": state,
                "day_kind": day.get("kind", "unknown"), "day_label": day.get("label", ""),
                "timezone": timezone, "manual_override": override,
                "provider_affected": affected, "evaluated_at": local}

    def uninstall(self):
        fish = self.instance
        if fish is not None and fish._cfg is self._cfg_wrapper:
            fish._cfg = self._original_cfg
