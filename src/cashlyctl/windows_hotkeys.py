from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import ctypes
import sys
from typing import Callable

from cashlyctl.crm_auth import (
    CrmAuthError,
    autodialer_macro_specs,
    send_autodialer_macro,
)
from cashlyctl.hotkeys import UNASSIGNED_HOTKEY, autodialer_macro_hotkey


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
SW_MINIMIZE = 6

_MODIFIER_NAMES = {
    "ALT": MOD_ALT,
    "CONTROL": MOD_CONTROL,
    "CTRL": MOD_CONTROL,
    "SHIFT": MOD_SHIFT,
    "WIN": MOD_WIN,
    "WINDOWS": MOD_WIN,
    "META": MOD_WIN,
}

_VIRTUAL_KEY_NAMES = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
}
_VIRTUAL_KEY_NAMES.update({f"F{index}": 0x6F + index for index in range(1, 25)})


class WindowsHotkeyError(RuntimeError):
    """Raised when Windows global hotkey registration cannot run."""


@dataclass(frozen=True, slots=True)
class WindowsHotkeyBinding:
    hotkey_id: int
    action: str
    label: str
    command: str
    accelerator: str
    modifiers: int
    virtual_key: int


def configured_windows_hotkey_bindings() -> tuple[WindowsHotkeyBinding, ...]:
    bindings: list[WindowsHotkeyBinding] = []
    for index, spec in enumerate(autodialer_macro_specs(), start=1):
        accelerator = autodialer_macro_hotkey(spec.action)
        if accelerator.strip().lower() == UNASSIGNED_HOTKEY:
            continue
        modifiers, virtual_key = parse_windows_hotkey(accelerator)
        bindings.append(
            WindowsHotkeyBinding(
                hotkey_id=1000 + index,
                action=spec.action,
                label=spec.label,
                command=spec.command,
                accelerator=accelerator,
                modifiers=modifiers,
                virtual_key=virtual_key,
            )
        )
    return tuple(bindings)


def parse_windows_hotkey(accelerator: str) -> tuple[int, int]:
    parts = [part.strip() for part in accelerator.split("+") if part.strip()]
    if not parts:
        raise WindowsHotkeyError("Hotkey binding is empty.")

    modifiers = 0
    virtual_key: int | None = None
    for part in parts:
        token = part.upper()
        modifier = _MODIFIER_NAMES.get(token)
        if modifier is not None:
            modifiers |= modifier
            continue
        if virtual_key is not None:
            raise WindowsHotkeyError(f"Hotkey {accelerator!r} has more than one key.")
        virtual_key = _virtual_key_for_token(token)

    if virtual_key is None:
        raise WindowsHotkeyError(f"Hotkey {accelerator!r} does not include a key.")
    if modifiers == 0:
        raise WindowsHotkeyError(f"Hotkey {accelerator!r} must include a modifier.")
    return modifiers, virtual_key


def run_windows_hotkey_listener(
    *,
    minimize_console: bool = False,
    emit: Callable[[str], None] | None = None,
) -> None:
    if sys.platform != "win32":
        raise WindowsHotkeyError("Windows global hotkeys require a native Windows runtime.")

    bindings = configured_windows_hotkey_bindings()
    if not bindings:
        raise WindowsHotkeyError("No autodialer hotkeys are assigned.")

    user32, kernel32, wintypes = _load_windows_api()
    if minimize_console:
        _minimize_console(user32, kernel32)

    emit_line = emit or print
    registered: list[WindowsHotkeyBinding] = []
    by_id = {binding.hotkey_id: binding for binding in bindings}
    try:
        for binding in bindings:
            _register_hotkey(user32, binding)
            registered.append(binding)
            emit_line(
                "registered "
                f"action={binding.action} hotkey={binding.accelerator} command={binding.command}"
            )

        emit_line("cashlyctl Windows hotkeys running. Press Ctrl+C in this window to stop.")
        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == -1:
                raise WindowsHotkeyError(f"GetMessageW failed with Win32 error {ctypes.get_last_error()}.")
            if result == 0:
                break
            if message.message == WM_HOTKEY:
                binding = by_id.get(int(message.wParam))
                if binding:
                    _dispatch_binding(binding, emit_line)
                continue
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    except KeyboardInterrupt:
        emit_line("cashlyctl Windows hotkeys stopping.")
    finally:
        for binding in registered:
            user32.UnregisterHotKey(None, binding.hotkey_id)


def _dispatch_binding(binding: WindowsHotkeyBinding, emit: Callable[[str], None]) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        command = send_autodialer_macro(binding.action)
    except (CrmAuthError, ValueError) as exc:
        emit(f"{timestamp} dispatch=fail action={binding.action} error={exc}")
        return

    emit(
        f"{timestamp} dispatch=ok action={binding.action} "
        f"command_id={command.command_id} status={command.status}"
    )


def _register_hotkey(user32: object, binding: WindowsHotkeyBinding) -> None:
    modifiers = binding.modifiers | MOD_NOREPEAT
    if user32.RegisterHotKey(None, binding.hotkey_id, modifiers, binding.virtual_key):
        return
    error = ctypes.get_last_error()
    raise WindowsHotkeyError(
        f"Failed to register {binding.accelerator} for {binding.action} "
        f"(Win32 error {error})."
    )


def _load_windows_api() -> tuple[object, object, object]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    return user32, kernel32, wintypes


def _minimize_console(user32: object, kernel32: object) -> None:
    handle = kernel32.GetConsoleWindow()
    if handle:
        user32.ShowWindow(handle, SW_MINIMIZE)


def _virtual_key_for_token(token: str) -> int:
    if len(token) == 1 and ("A" <= token <= "Z" or "0" <= token <= "9"):
        return ord(token)
    virtual_key = _VIRTUAL_KEY_NAMES.get(token)
    if virtual_key is None:
        raise WindowsHotkeyError(f"Unsupported Windows hotkey key: {token}.")
    return virtual_key
