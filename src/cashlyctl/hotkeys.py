from __future__ import annotations

from cashlyctl.runtime_env import runtime_env


DEFAULT_NEXT_CONTACT_HOTKEY = "Ctrl+Shift+S"


def next_contact_hotkey() -> str:
    value = runtime_env("CASHLYCTL_HOTKEY_NEXT_CONTACT", DEFAULT_NEXT_CONTACT_HOTKEY).strip()
    return value or DEFAULT_NEXT_CONTACT_HOTKEY
