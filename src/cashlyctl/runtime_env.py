from __future__ import annotations

import os
from pathlib import Path

from cashlyctl.paths import default_app_home


ENV_FILE_ENV = "CASHLYCTL_ENV_FILE"

_RUNTIME_ENV_CACHE: dict[str, str] | None = None
_RUNTIME_ENV_SOURCE: Path | None = None


def runtime_env(key: str, default: str = "") -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    file_value = load_runtime_env_file().get(key, "").strip()
    return file_value if file_value else default


def load_runtime_env_file() -> dict[str, str]:
    global _RUNTIME_ENV_CACHE, _RUNTIME_ENV_SOURCE
    if _RUNTIME_ENV_CACHE is not None:
        return _RUNTIME_ENV_CACHE

    for env_path in runtime_env_file_candidates():
        if not env_path.exists():
            continue
        _RUNTIME_ENV_CACHE = _parse_env_file(env_path)
        _RUNTIME_ENV_SOURCE = env_path
        return _RUNTIME_ENV_CACHE

    _RUNTIME_ENV_CACHE = {}
    _RUNTIME_ENV_SOURCE = None
    return _RUNTIME_ENV_CACHE


def runtime_env_file_source() -> Path | None:
    load_runtime_env_file()
    return _RUNTIME_ENV_SOURCE


def runtime_env_file_candidates() -> list[Path]:
    explicit = os.getenv(ENV_FILE_ENV, "").strip()
    if explicit:
        return [_expand_path(explicit)]

    app_home = default_app_home()
    candidates = [
        app_home / ".env",
        Path("/app/.env"),
        Path(".env"),
    ]

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return data

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        env_key, value = line.split("=", 1)
        key_clean = env_key.strip()
        value_clean = value.strip().strip('"').strip("'")
        if key_clean and value_clean:
            data[key_clean] = value_clean
    return data


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()
