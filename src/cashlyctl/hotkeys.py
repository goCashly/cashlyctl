from __future__ import annotations

from cashlyctl.runtime_env import runtime_env


DEFAULT_NEXT_CONTACT_HOTKEY = "Ctrl+Shift+S"
UNASSIGNED_HOTKEY = "unassigned"

_HOTKEY_ENV_KEYS = {
    "start": "CASHLYCTL_HOTKEY_AUTODIALER_START",
    "next-contact": "CASHLYCTL_HOTKEY_NEXT_CONTACT",
    "pause": "CASHLYCTL_HOTKEY_AUTODIALER_PAUSE",
    "resume": "CASHLYCTL_HOTKEY_AUTODIALER_RESUME",
    "stop": "CASHLYCTL_HOTKEY_AUTODIALER_STOP",
}

_HOTKEY_DEFAULTS = {
    "next-contact": DEFAULT_NEXT_CONTACT_HOTKEY,
}


def next_contact_hotkey() -> str:
    return autodialer_macro_hotkey("next-contact")


def autodialer_macro_hotkey(action: str) -> str:
    key = action.strip().lower().replace("_", "-")
    env_key = _HOTKEY_ENV_KEYS.get(key)
    fallback = _HOTKEY_DEFAULTS.get(key, UNASSIGNED_HOTKEY)
    if not env_key:
        return fallback
    value = runtime_env(env_key, fallback).strip()
    return value or fallback
