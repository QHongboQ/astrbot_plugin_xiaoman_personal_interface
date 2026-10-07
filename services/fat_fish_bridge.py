"""Isolate native and Fat Fish 1.1.1 compatibility policy integration."""
from __future__ import annotations

from datetime import datetime
import importlib
from types import MethodType
from zoneinfo import ZoneInfo

FAT_FISH_NAME = "astrbot_plugin_fat_fish_wallet"
MARKER = "_xiaoman_wallet_bridge_installed"


class FatFishBridge:
    def __init__(self, context, day_adapter):
        self.context, self.day_adapter = context, day_adapter
        self.instance = None
        self._original_cfg = None
        self._cfg_wrapper = None
        self._added_policy = None
        self._native = False

    def discover(self):
        try:
            for metadata in self.context.get_all_stars():
                if getattr(metadata, "name", None) == FAT_FISH_NAME:
                    if getattr(metadata, "activated", False) is True:
                        return getattr(metadata, "star_cls", None)
        except Exception:
            return None
        return None

    def version(self):
        try:
            return next((str(getattr(meta, "version", "") or "") for meta in self.context.get_all_stars()
                         if getattr(meta, "name", None) == FAT_FISH_NAME
                         and getattr(meta, "activated", False) is True), "")
        except Exception:
            return ""

    def install(self):
        fish = self.discover()
        if fish is None:
            return False
        self.instance = fish
        if getattr(fish, MARKER, False):
            return True
        native = getattr(fish, "get_wallet_policy", None)
        if callable(native):
            self._native = True
            return True
        cfg = getattr(fish, "_cfg", None)
        if not callable(cfg):
            return False
        self._original_cfg = cfg
        bridge = self
        def cfg_wrapper(key, *args, **kwargs):
            return bridge._wrapped_cfg(key, *args, **kwargs)
        self._cfg_wrapper = cfg_wrapper
        setattr(fish, MARKER, True)
        fish._cfg = self._cfg_wrapper
        if not callable(getattr(fish, "get_wallet_policy", None)):
            self._added_policy = MethodType(lambda _fish, **kwargs: self.get_wallet_policy(**kwargs), fish)
            fish.get_wallet_policy = self._added_policy
        return True

    def _wrapped_cfg(self, key, *args, **kwargs):
        value = self._original_cfg(key, *args, **kwargs)
        if key != "manual_override" or value != "auto":
            return value
        policy = self._compat_policy(at=None, provider_id=None)
        if policy.get("day_kind") in {"holiday", "weekend"}:
            return "always_allow"
        return value

    def _config_value(self, key, default=None):
        if key == "manual_override" and self._original_cfg is not None:
            try: return self._original_cfg(key, default)
            except TypeError:
                try: return self._original_cfg(key)
                except Exception: pass
            except Exception: pass
        try:
            return self.instance._cfg(key, default)
        except TypeError:
            try:
                return self.instance._cfg(key)
            except Exception:
                return default
        except Exception:
            return default

    def _compat_policy(self, *, at=None, provider_id=None):
        if not callable(getattr(self.instance, "_cfg", None)):
            return {"found": True, "native_policy": False, "policy_mode": "compat_unavailable",
                    "enabled": False, "allowed": False, "state": "compat_unavailable",
                    "timezone": "", "manual_override": "unknown", "provider_affected": False,
                    "day_kind": "unknown", "day_label": "", "holiday": False,
                    "holiday_name": "", "peak_periods": [], "peak_weekdays": [], "evaluated_at": at}
        now = at or datetime.now().astimezone()
        day = self.day_adapter.get_day_policy(now)
        enabled_value = self._config_value("enabled", None)
        if enabled_value is None:
            enabled_value = self._config_value("enable", True)
        enabled = bool(enabled_value)
        override = str(self._config_value("manual_override", "auto") or "auto")
        timezone = str(self._config_value("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            zone = ZoneInfo(timezone)
            local = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
        except Exception:
            local = now
        provider_affected = True
        match = getattr(self.instance, "_provider_affected", None)
        if callable(match):
            provider = None
            if provider_id:
                try: provider = self.context.get_provider_by_id(provider_id)
                except Exception: pass
            try:
                provider_affected = bool(match(provider_id, provider))
            except TypeError:
                try: provider_affected = bool(match(provider_id))
                except Exception: provider_affected = True
            except Exception:
                provider_affected = True
        periods = self.instance._periods() if callable(getattr(self.instance, "_periods", None)) else []
        weekdays = self.instance._weekdays() if callable(getattr(self.instance, "_weekdays", None)) else list(range(7))
        state, allowed = "offpeak", True
        if not enabled:
            state, allowed = "disabled", True
        elif override == "always_allow":
            state, allowed = "forced_allow", True
        elif override == "always_block":
            state, allowed = "forced_block", False
        elif day.get("available") and day.get("kind") in {"holiday", "weekend"}:
            state, allowed = "offpeak", True
        elif provider_affected:
            try:
                in_peak = self._is_peak(local, periods, weekdays)
                if in_peak is None:
                    return {"found": True, "native_policy": False, "policy_mode": "compat_unavailable",
                            "enabled": enabled, "allowed": False, "state": "compat_unavailable",
                            "timezone": timezone, "manual_override": override, "provider_affected": provider_affected,
                            "day_kind": day.get("kind", "unknown"), "day_label": day.get("label", ""),
                            "holiday": day.get("kind") == "holiday", "holiday_name": "",
                            "peak_periods": periods, "peak_weekdays": weekdays, "evaluated_at": local}
                if in_peak:
                    state, allowed = "peak", False
            except Exception:
                state, allowed = "unknown", True
        return {"found": True, "native_policy": False, "enabled": enabled, "allowed": allowed,
                "policy_mode": "compat",
                "state": state, "timezone": timezone, "manual_override": override,
                "provider_affected": provider_affected, "day_kind": day.get("kind", "unknown"),
                "day_label": day.get("label", ""), "holiday": day.get("kind") == "holiday",
                "holiday_name": day.get("label", "") if day.get("kind") == "holiday" else "",
                "peak_periods": periods, "peak_weekdays": weekdays, "evaluated_at": local}

    def _is_peak(self, local, periods, weekdays):
        helper = getattr(self.instance, "is_peak", None) or getattr(self.instance, "_is_peak", None)
        if callable(helper):
            try: return bool(helper(local, periods, weekdays))
            except Exception: return None
        try:
            module_name = self.instance.__class__.__module__.rsplit(".", 1)[0] + ".scheduler"
            scheduler = importlib.import_module(module_name)
            return bool(scheduler.is_peak(local, periods, weekdays))
        except Exception:
            return None

    def get_wallet_policy(self, *, at=None, provider_id=None):
        fish = self.discover()
        if fish is None:
            return {"found": False, "enabled": False, "allowed": False, "state": "missing", "day_kind": "unknown"}
        self.instance = fish
        native = getattr(fish, "get_wallet_policy", None)
        if callable(native) and native != self._added_policy:
            try:
                policy = dict(native(at=at, provider_id=provider_id))
                day = self.day_adapter.get_day_policy(at or policy.get("evaluated_at"))
                policy.update({"found": True, "native_policy": True, "policy_mode": "native",
                               "day_kind": day.get("kind", "unknown"), "day_label": day.get("label", ""),
                               "holiday": day.get("kind") == "holiday",
                               "holiday_name": day.get("label", "") if day.get("kind") == "holiday" else ""})
                return policy
            except Exception:
                return {"found": True, "native_policy": True, "enabled": True, "allowed": False, "state": "policy_error"}
        return self._compat_policy(at=at, provider_id=provider_id)

    def uninstall(self):
        fish = self.instance
        if fish is None or self._native:
            return
        current = getattr(fish, "_cfg", None)
        wrapper_still_installed = current is self._cfg_wrapper or (
            getattr(current, "__func__", None) is getattr(self._cfg_wrapper, "__func__", None)
            and getattr(current, "__self__", None) is fish
        )
        if wrapper_still_installed:
            fish._cfg = self._original_cfg
        if self._added_policy is not None and getattr(fish, "get_wallet_policy", None) is self._added_policy:
            delattr(fish, "get_wallet_policy")
        if getattr(fish, MARKER, False):
            delattr(fish, MARKER)
