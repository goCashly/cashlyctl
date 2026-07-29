from __future__ import annotations

import json
import time

import typer

from cashlyctl.auth import (
    has_local_users,
    load_login_credentials,
    role_for_login_user,
    verify_local_login,
    verify_login,
)
from cashlyctl.config import load_config
from cashlyctl.console_app import CashlyConsoleApp
from cashlyctl.crm_auth import (
    CrmAuthError,
    autodialer_macro_specs,
    attempt_open_pairing_url,
    default_device_label,
    forget_crm_device_session,
    load_crm_device_session,
    poll_crm_pairing,
    save_crm_device_session,
    send_autodialer_macro,
    start_crm_pairing,
    verify_crm_device_session,
)
from cashlyctl.health import run_mvp_checks
from cashlyctl.host_inspect import format_host_inspection, inspect_host
from cashlyctl.hotkeys import autodialer_macro_hotkey
from cashlyctl.windows_hotkeys import (
    WindowsHotkeyError,
    configured_windows_hotkey_bindings,
    run_windows_hotkey_listener,
)


app = typer.Typer(help="cashlyctl operations console and CLI")
profile_app = typer.Typer(help="Profile commands")
system_app = typer.Typer(help="System inspection and diagnostics")
crm_app = typer.Typer(help="CashlyCRM browser pairing and device auth")
hotkeys_app = typer.Typer(help="Host hotkey configuration and diagnostics")
app.add_typer(profile_app, name="profile")
app.add_typer(system_app, name="system")
app.add_typer(crm_app, name="crm")
app.add_typer(hotkeys_app, name="hotkeys")


@app.command("console")
def run_console() -> None:
    """Launch the interactive Textual console."""
    CashlyConsoleApp().run()


@profile_app.command("list")
def list_profiles() -> None:
    """List configured profiles."""
    config = load_config()
    for profile in config.profiles:
        marker = "*" if profile.name == config.active_profile else " "
        print(
            f"[{marker}] {profile.name} "
            f"(env={profile.env.value}, mode={profile.mode.value}, "
            f"control_api={profile.control_api_base_url})"
        )


@app.command("health")
def health(profile: str | None = typer.Option(None, "--profile", "-p")) -> None:
    """Run MVP health checks for active or selected profile."""
    config = load_config()
    active = config.get_profile(profile) if profile else config.get_active_profile()
    if not active:
        raise typer.BadParameter(f"Profile not found: {profile}")
    results = run_mvp_checks(active)
    print(f"profile={active.name} env={active.env.value} mode={active.mode.value}")
    for result in results:
        print(
            f"- {result.name}: {result.status.value} "
            f"latency_ms={result.latency_ms} detail={result.detail}"
        )


@system_app.command("inspect-host")
def inspect_host_command(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Inspect runtime OS, host hints, container state, and hotkey support."""
    report = inspect_host()
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    print(format_host_inspection(report))


@crm_app.command("pair")
def pair_crm(
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="CashlyCRM base URL. Defaults to CASHLYCTL_CRM_BASE_URL or production.",
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        help="Device label shown in CashlyCRM approval records.",
    ),
    timeout_seconds: int = typer.Option(600, "--timeout", min=30, help="Pairing timeout."),
    open_browser: bool = typer.Option(False, "--open-browser", help="Open approval URL locally."),
) -> None:
    """Pair this local cashlyctl install with a logged-in CashlyCRM browser."""
    user, role = _require_local_admin_auth()
    typer.echo(f"local_auth=ok user={user} role={role} mode=MAINT")
    try:
        start = start_crm_pairing(base_url=base_url, device_label=label or default_device_label())
    except CrmAuthError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"open_url={start.verification_uri}")
    typer.echo(f"user_code={start.user_code}")
    if open_browser:
        opened, open_detail = attempt_open_pairing_url(start.verification_uri)
        typer.echo(f"browser_open={str(opened).lower()} detail={open_detail}")
    typer.echo("waiting_for_browser_approval=true")

    deadline = time.monotonic() + min(max(timeout_seconds, 30), start.expires_in)
    interval = start.interval
    while time.monotonic() < deadline:
        poll = poll_crm_pairing(start.base_url, start.device_code)
        if poll.status == "approved" and poll.token:
            session = save_crm_device_session(
                start.base_url,
                poll.token,
                poll.token_type,
                poll.device,
            )
            device = session.device
            typer.echo(f"paired=true device_id={device.get('id', '-')}")
            typer.echo(f"organization_id={device.get('organizationId', '-')}")
            typer.echo("token_stored=true")
            return
        if poll.status not in {"pending", "authorization_pending"}:
            raise typer.BadParameter(poll.error or f"Pairing failed: {poll.status}")
        time.sleep(max(1, poll.interval or interval))

    raise typer.BadParameter("Timed out waiting for browser approval.")


@crm_app.command("whoami")
def crm_whoami() -> None:
    """Verify the stored CashlyCRM device session."""
    try:
        data = verify_crm_device_session()
    except CrmAuthError as exc:
        raise typer.BadParameter(str(exc)) from exc
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    organization = data.get("organization") if isinstance(data.get("organization"), dict) else {}
    typer.echo(f"device_id={device.get('id', '-')}")
    typer.echo(f"profile={profile.get('email') or profile.get('fullName') or profile.get('id', '-')}")
    typer.echo(f"organization={organization.get('name') or organization.get('id', '-')}")


@crm_app.command("forget")
def crm_forget() -> None:
    """Remove the locally stored CashlyCRM device token."""
    removed = forget_crm_device_session()
    typer.echo("removed=true" if removed else "removed=false")


@crm_app.command("status")
def crm_status() -> None:
    """Show whether a local CashlyCRM device token is stored."""
    session = load_crm_device_session()
    if not session:
        typer.echo("paired=false")
        return
    typer.echo("paired=true")
    typer.echo(f"base_url={session.base_url}")
    typer.echo(f"paired_at={session.paired_at or '-'}")
    typer.echo(f"device_id={session.device.get('id', '-')}")


@crm_app.command("next-contact")
def crm_next_contact() -> None:
    """Queue the autodialer next-contact macro for the paired CashlyCRM browser."""
    _queue_crm_macro("next-contact")


@crm_app.command("start")
def crm_start() -> None:
    """Queue the autodialer start macro for the paired CashlyCRM browser."""
    _queue_crm_macro("start")


@crm_app.command("pause")
def crm_pause() -> None:
    """Queue the autodialer pause macro for the paired CashlyCRM browser."""
    _queue_crm_macro("pause")


@crm_app.command("resume")
def crm_resume() -> None:
    """Queue the autodialer resume macro for the paired CashlyCRM browser."""
    _queue_crm_macro("resume")


@crm_app.command("stop")
def crm_stop() -> None:
    """Queue the autodialer stop macro for the paired CashlyCRM browser."""
    _queue_crm_macro("stop")


@crm_app.command("macro")
def crm_macro(action: str = typer.Argument(..., help="Autodialer action to queue.")) -> None:
    """Queue an autodialer macro by action name."""
    _queue_crm_macro(action)


def _queue_crm_macro(action: str) -> None:
    try:
        command = send_autodialer_macro(action)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except CrmAuthError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo("queued=true")
    typer.echo(f"command_id={command.command_id}")
    typer.echo(f"type={command.command_type}")
    typer.echo(f"status={command.status}")
    typer.echo(f"expires_at={command.expires_at or '-'}")


@hotkeys_app.command("status")
def hotkeys_status() -> None:
    """Show configured hotkey bindings and host support guidance."""
    report = inspect_host()
    for spec in autodialer_macro_specs():
        key = spec.action.replace("-", "_")
        typer.echo(f"{key}={autodialer_macro_hotkey(spec.action)}")
    typer.echo(f"hotkey_support={report.hotkey_support}")
    typer.echo(f"recommended_backend={report.recommended_backend}")
    typer.echo(f"containerized={str(report.is_container).lower()}")


@hotkeys_app.command("start")
def hotkeys_start(
    backend: str = typer.Option("auto", "--backend", help="Hotkey backend: auto or windows."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print bindings without registering them."),
    minimize_console: bool = typer.Option(
        False,
        "--minimize-console",
        help="Minimize the console window after Windows hotkeys are registered.",
    ),
) -> None:
    """Start the native hotkey helper."""
    report = inspect_host()
    selected_backend = backend.strip().lower()
    if selected_backend == "auto":
        selected_backend = (
            "windows" if report.recommended_backend == "windows" else report.recommended_backend
        )

    if selected_backend not in {"windows"}:
        if dry_run:
            _print_windows_hotkey_bindings()
            return
        typer.echo(format_host_inspection(report))
        raise typer.BadParameter(
            "The native cashlyctl hotkey listener is implemented for Windows first. "
            "Use desktop shortcut bindings on Linux/Wayland for now."
        )

    if dry_run:
        _print_windows_hotkey_bindings()
        return

    typer.echo("cashlyctl_windows_hotkeys=starting")
    try:
        run_windows_hotkey_listener(minimize_console=minimize_console, emit=typer.echo)
    except WindowsHotkeyError as exc:
        raise typer.BadParameter(str(exc)) from exc


@hotkeys_app.command("stop")
def hotkeys_stop() -> None:
    """Explain how to stop the foreground hotkey helper."""
    typer.echo("managed_daemon=false")
    typer.echo("Stop a running foreground helper with Ctrl+C or by closing its window.")
    typer.echo("If installed as a startup shortcut, disable CashlyCTL Hotkeys in Windows Startup Apps.")


@hotkeys_app.command("doctor")
def hotkeys_doctor() -> None:
    """Explain whether this runtime can register system-wide hotkeys."""
    report = inspect_host()
    for spec in autodialer_macro_specs():
        key = spec.action.replace("-", "_")
        typer.echo(f"{key}={autodialer_macro_hotkey(spec.action)}")
    typer.echo(format_host_inspection(report))


def _print_windows_hotkey_bindings() -> None:
    for binding in configured_windows_hotkey_bindings():
        typer.echo(
            f"{binding.action}={binding.accelerator} command={binding.command} "
            f"vk={binding.virtual_key}"
        )


def _require_local_admin_auth() -> tuple[str, str]:
    if not has_local_users() and not load_login_credentials():
        raise typer.BadParameter("No local users. Run `cashlyctl console`, then INITADMIN.")
    username = typer.prompt("Local cashlyctl user").strip()
    password = typer.prompt("Local cashlyctl password", hide_input=True)
    local = verify_local_login(username, password)
    if local:
        return local.username, local.role
    credentials = load_login_credentials()
    if verify_login(username, password, credentials):
        return username.strip().lower(), role_for_login_user(username)
    raise typer.BadParameter("Local cashlyctl authentication failed.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
