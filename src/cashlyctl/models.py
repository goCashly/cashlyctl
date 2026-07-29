from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Environment(StrEnum):
    DEV = "dev"
    STAGE = "stage"
    PROD = "prod"


class DeploymentMode(StrEnum):
    INTERNAL = "internal"
    ENTERPRISE = "enterprise"


class HealthStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(slots=True)
class Profile:
    name: str
    env: Environment
    mode: DeploymentMode
    control_api_base_url: str
    auth_method: str = "token"
    credential_ref: str = "local"
    neo4j_bolt_uri: str | None = None
    dealsense_url: str | None = None
    proxmox_api_url: str | None = None

    @staticmethod
    def from_dict(raw: dict[str, object]) -> "Profile":
        return Profile(
            name=str(raw["name"]),
            env=Environment(str(raw["env"]).lower()),
            mode=DeploymentMode(str(raw["mode"]).lower()),
            control_api_base_url=str(raw.get("control_api_base_url", "")),
            auth_method=str(raw.get("auth_method", "token")),
            credential_ref=str(raw.get("credential_ref", "local")),
            neo4j_bolt_uri=_str_or_none(raw.get("neo4j_bolt_uri")),
            dealsense_url=_str_or_none(raw.get("dealsense_url")),
            proxmox_api_url=_str_or_none(raw.get("proxmox_api_url")),
        )

    def to_dict(self) -> dict[str, str]:
        data: dict[str, str] = {
            "name": self.name,
            "env": self.env.value,
            "mode": self.mode.value,
            "control_api_base_url": self.control_api_base_url,
            "auth_method": self.auth_method,
            "credential_ref": self.credential_ref,
        }
        if self.neo4j_bolt_uri:
            data["neo4j_bolt_uri"] = self.neo4j_bolt_uri
        if self.dealsense_url:
            data["dealsense_url"] = self.dealsense_url
        if self.proxmox_api_url:
            data["proxmox_api_url"] = self.proxmox_api_url
        return data


@dataclass(slots=True)
class AppConfig:
    active_profile: str
    profiles: list[Profile] = field(default_factory=list)

    def get_profile(self, name: str) -> Profile | None:
        target = name.strip().lower()
        for profile in self.profiles:
            if profile.name.lower() == target:
                return profile
        return None

    def get_active_profile(self) -> Profile:
        profile = self.get_profile(self.active_profile)
        if not profile:
            raise ValueError(f"Active profile '{self.active_profile}' not found")
        return profile


@dataclass(slots=True)
class HealthCheckResult:
    name: str
    status: HealthStatus
    latency_ms: int
    detail: str
    timestamp: datetime


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None

