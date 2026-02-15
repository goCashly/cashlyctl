from __future__ import annotations

import typer

from cashlyctl.config import load_config
from cashlyctl.console_app import CashlyConsoleApp
from cashlyctl.health import run_mvp_checks


app = typer.Typer(help="cashlyctl operations console and CLI")
profile_app = typer.Typer(help="Profile commands")
app.add_typer(profile_app, name="profile")


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()

