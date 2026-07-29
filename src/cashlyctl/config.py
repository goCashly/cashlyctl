from __future__ import annotations

import tomllib

from cashlyctl.models import AppConfig, DeploymentMode, Environment, Profile
from cashlyctl.paths import default_app_home


APP_HOME = default_app_home()
CONFIG_PATH = APP_HOME / "config.toml"
CATALOG_DIR = APP_HOME / "catalog"
QUERIES_DIR = APP_HOME / "queries"
STATE_DIR = APP_HOME / "state"
LOGS_DIR = APP_HOME / "logs"


def ensure_state_layout() -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    QUERIES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def default_config() -> AppConfig:
    return AppConfig(
        active_profile="local-dev",
        profiles=[
            Profile(
                name="local-dev",
                env=Environment.DEV,
                mode=DeploymentMode.INTERNAL,
                control_api_base_url="http://127.0.0.1:8080",
                auth_method="local",
                credential_ref="local-dev",
                neo4j_bolt_uri="bolt://127.0.0.1:7687",
                dealsense_url="http://127.0.0.1:8090",
            ),
            Profile(
                name="enterprise-stage",
                env=Environment.STAGE,
                mode=DeploymentMode.ENTERPRISE,
                control_api_base_url="https://control.example.com",
                auth_method="device_code",
                credential_ref="enterprise-stage",
            ),
        ],
    )


def load_config() -> AppConfig:
    ensure_state_layout()
    if not CONFIG_PATH.exists():
        config = default_config()
        save_config(config)
        return config

    with CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)
    active_profile = str(raw.get("active_profile", ""))
    profiles_raw = raw.get("profiles", [])
    if not isinstance(profiles_raw, list):
        raise ValueError("Invalid config: 'profiles' must be an array")
    profiles = [Profile.from_dict(item) for item in profiles_raw if isinstance(item, dict)]
    if not active_profile and profiles:
        active_profile = profiles[0].name
    return AppConfig(active_profile=active_profile, profiles=profiles)


def save_config(config: AppConfig) -> None:
    ensure_state_layout()
    lines: list[str] = [f'active_profile = "{_esc(config.active_profile)}"', ""]
    for profile in config.profiles:
        data = profile.to_dict()
        lines.append("[[profiles]]")
        for key in [
            "name",
            "env",
            "mode",
            "control_api_base_url",
            "auth_method",
            "credential_ref",
            "neo4j_bolt_uri",
            "dealsense_url",
            "proxmox_api_url",
        ]:
            value = data.get(key)
            if value is None:
                continue
            lines.append(f'{key} = "{_esc(value)}"')
        lines.append("")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
