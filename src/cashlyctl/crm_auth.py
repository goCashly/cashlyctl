from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import socket
import time
from typing import Callable
import urllib.error
import urllib.request
import webbrowser

from cashlyctl.auth import LOCAL_AUTH_DIR
from cashlyctl.host_inspect import inspect_host
from cashlyctl.runtime_env import runtime_env


DEFAULT_CRM_BASE_URL = "https://crm.gocashly.io"
CRM_DEVICE_SESSION_PATH = LOCAL_AUTH_DIR / "cashlycrm_device.json"


class CrmAuthError(RuntimeError):
    """Raised when CashlyCRM browser/device auth cannot continue."""


@dataclass(frozen=True, slots=True)
class CrmPairingStart:
    base_url: str
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    scopes: list[str]


@dataclass(frozen=True, slots=True)
class CrmPairingPoll:
    status: str
    token: str = ""
    token_type: str = "Bearer"
    device: dict[str, object] | None = None
    interval: int = 3
    error: str = ""


@dataclass(frozen=True, slots=True)
class CrmDeviceSession:
    base_url: str
    token: str
    token_type: str
    device: dict[str, object]
    paired_at: str


@dataclass(frozen=True, slots=True)
class CrmDeviceCommand:
    command_id: str
    command_type: str
    status: str
    expires_at: str


def crm_base_url(value: str | None = None) -> str:
    raw = (value or runtime_env("CASHLYCTL_CRM_BASE_URL", DEFAULT_CRM_BASE_URL)).strip()
    return raw.rstrip("/") or DEFAULT_CRM_BASE_URL


def default_device_label() -> str:
    hostname = socket.gethostname().strip() or "local-host"
    return f"cashlyctl on {hostname}"


def attempt_open_pairing_url(url: str) -> tuple[bool, str]:
    if not _auto_open_enabled():
        return False, "AUTO OPEN DISABLED: CASHLYCTL_CRM_AUTO_OPEN_BROWSER=0"

    report = inspect_host()
    force_container = _truthy(runtime_env("CASHLYCTL_CRM_FORCE_OPEN_BROWSER", ""))
    if report.is_container and not force_container:
        return (
            False,
            "AUTO OPEN SKIPPED: container cannot launch the host browser; open the URL manually",
        )

    try:
        opened = webbrowser.open(url, new=2)
    except Exception as exc:
        return False, f"AUTO OPEN FAILED: {exc}"

    if opened:
        return True, "BROWSER OPENED"
    return False, "AUTO OPEN FAILED: no browser handler available"


def start_crm_pairing(
    base_url: str | None = None,
    device_label: str | None = None,
    scopes: list[str] | None = None,
) -> CrmPairingStart:
    resolved_base_url = crm_base_url(base_url)
    payload = {
        "deviceLabel": (device_label or default_device_label()).strip(),
        "host": _host_payload(),
        "scopes": scopes or ["hotkeys", "autodialer:control"],
    }
    status, data = _post_json(f"{resolved_base_url}/api/cashlyctl/pair/start", payload)
    if status != 200:
        raise CrmAuthError(_error_message(data, "Failed to start CashlyCRM pairing."))
    try:
        return CrmPairingStart(
            base_url=resolved_base_url,
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(data["verification_uri"]),
            expires_in=int(data.get("expires_in", 600)),
            interval=max(1, int(data.get("interval", 3))),
            scopes=[str(item) for item in data.get("scopes", []) if str(item).strip()],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CrmAuthError("CashlyCRM returned an invalid pairing response.") from exc


def poll_crm_pairing(base_url: str, device_code: str) -> CrmPairingPoll:
    status, data = _post_json(
        f"{crm_base_url(base_url)}/api/cashlyctl/pair/poll",
        {"deviceCode": device_code},
    )
    if status == 202:
        return CrmPairingPoll(
            status=str(data.get("status", "pending")),
            interval=max(1, int(data.get("interval", 3))),
            error=str(data.get("error", "")),
        )
    if status != 200:
        return CrmPairingPoll(
            status=str(data.get("status") or data.get("error") or "error"),
            error=_error_message(data, "CashlyCRM pairing failed."),
        )
    token = str(data.get("token", "")).strip()
    if not token:
        return CrmPairingPoll(status="error", error="CashlyCRM did not return a device token.")
    return CrmPairingPoll(
        status=str(data.get("status", "approved")),
        token=token,
        token_type=str(data.get("tokenType", "Bearer")),
        device=data.get("device") if isinstance(data.get("device"), dict) else {},
    )


def save_crm_device_session(
    base_url: str,
    token: str,
    token_type: str,
    device: dict[str, object] | None,
) -> CrmDeviceSession:
    session = CrmDeviceSession(
        base_url=crm_base_url(base_url),
        token=token,
        token_type=token_type or "Bearer",
        device=device or {},
        paired_at=datetime.now(tz=UTC).isoformat(),
    )
    LOCAL_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCAL_AUTH_DIR.chmod(0o700)
    except OSError:
        pass
    CRM_DEVICE_SESSION_PATH.write_text(
        json.dumps(
            {
                "version": 1,
                "base_url": session.base_url,
                "token": session.token,
                "token_type": session.token_type,
                "device": session.device,
                "paired_at": session.paired_at,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        CRM_DEVICE_SESSION_PATH.chmod(0o600)
    except OSError:
        pass
    return session


def load_crm_device_session() -> CrmDeviceSession | None:
    if not CRM_DEVICE_SESSION_PATH.exists():
        return None
    try:
        raw = json.loads(CRM_DEVICE_SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    token = str(raw.get("token", "")).strip()
    if not token:
        return None
    return CrmDeviceSession(
        base_url=crm_base_url(str(raw.get("base_url", ""))),
        token=token,
        token_type=str(raw.get("token_type", "Bearer")),
        device=raw.get("device") if isinstance(raw.get("device"), dict) else {},
        paired_at=str(raw.get("paired_at", "")),
    )


def forget_crm_device_session() -> bool:
    try:
        CRM_DEVICE_SESSION_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


def verify_crm_device_session(session: CrmDeviceSession | None = None) -> dict[str, object]:
    active = session or load_crm_device_session()
    if not active:
        raise CrmAuthError("No CashlyCRM device session is stored. Run CRM PAIR first.")
    status, data = _get_json(
        f"{active.base_url}/api/cashlyctl/device/session",
        token=active.token,
    )
    if status != 200:
        raise CrmAuthError(_error_message(data, "CashlyCRM device session is not valid."))
    return data


def send_crm_device_command(
    command_type: str,
    payload: dict[str, object] | None = None,
    session: CrmDeviceSession | None = None,
) -> CrmDeviceCommand:
    active = session or load_crm_device_session()
    if not active:
        raise CrmAuthError("No CashlyCRM device session is stored. Run CRM PAIR first.")

    status, data = _post_json(
        f"{active.base_url}/api/cashlyctl/device/commands",
        {
            "commandType": command_type,
            "payload": payload or {},
        },
        token=active.token,
    )
    if status not in {200, 202}:
        raise CrmAuthError(_error_message(data, "CashlyCRM command was not accepted."))

    command = data.get("command")
    if not isinstance(command, dict):
        raise CrmAuthError("CashlyCRM returned an invalid command response.")

    command_id = str(command.get("id", "")).strip()
    resolved_type = str(command.get("type", "")).strip()
    if not command_id or not resolved_type:
        raise CrmAuthError("CashlyCRM returned an incomplete command response.")

    return CrmDeviceCommand(
        command_id=command_id,
        command_type=resolved_type,
        status=str(command.get("status", "pending")),
        expires_at=str(command.get("expiresAt", "")),
    )


def send_next_contact_macro(session: CrmDeviceSession | None = None) -> CrmDeviceCommand:
    return send_crm_device_command(
        "autodialer.next_contact",
        {"source": "cashlyctl", "macro": "next_contact"},
        session=session,
    )


def pair_until_approved(
    base_url: str | None = None,
    device_label: str | None = None,
    timeout_seconds: int = 600,
    on_status: Callable[[str], None] | None = None,
) -> CrmDeviceSession:
    start = start_crm_pairing(base_url=base_url, device_label=device_label)
    if on_status:
        on_status(f"Open: {start.verification_uri}")
        on_status(f"Code: {start.user_code}")

    deadline = time.monotonic() + min(max(1, timeout_seconds), start.expires_in)
    interval = start.interval
    while time.monotonic() < deadline:
        poll = poll_crm_pairing(start.base_url, start.device_code)
        if poll.status == "approved" and poll.token:
            return save_crm_device_session(
                start.base_url,
                poll.token,
                poll.token_type,
                poll.device,
            )
        if poll.status not in {"pending", "authorization_pending"}:
            raise CrmAuthError(poll.error or f"CashlyCRM pairing failed: {poll.status}")
        if on_status:
            on_status("Waiting for browser approval...")
        interval = max(1, poll.interval or interval)
        time.sleep(interval)
    raise CrmAuthError("CashlyCRM pairing timed out before browser approval.")


def _post_json(
    url: str,
    payload: dict[str, object],
    token: str | None = None,
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    return _open_json(request)


def _get_json(url: str, token: str) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    return _open_json(request)


def _open_json(request: urllib.request.Request) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, _decode_json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode_json(exc.read())
    except urllib.error.URLError as exc:
        raise CrmAuthError(f"Unable to reach CashlyCRM: {exc.reason}") from exc
    except TimeoutError as exc:
        raise CrmAuthError("CashlyCRM request timed out.") from exc


def _decode_json(raw: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _error_message(data: dict[str, object], fallback: str) -> str:
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    message = data.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return fallback


def _auto_open_enabled() -> bool:
    value = runtime_env("CASHLYCTL_CRM_AUTO_OPEN_BROWSER", "1")
    return not _falsey(value)


def _falsey(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _host_payload() -> dict[str, object]:
    report = inspect_host().to_dict()
    return {
        "os": str(report.get("host_os") or report.get("runtime_os") or ""),
        "runtime": str(report.get("runtime_os") or ""),
        "hostname": socket.gethostname(),
        "container": bool(report.get("containerized")),
    }
