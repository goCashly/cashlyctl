from __future__ import annotations

import os
from pathlib import Path


LOGIN_PREFIX = "CASHLYCTL_LOGIN_"


def load_login_credentials(env_file: str = ".env") -> dict[str, str]:
    creds: dict[str, str] = {}
    env_path = Path(env_file)
    if env_path.exists():
        creds.update(_parse_env_credentials(env_path))

    for key, value in os.environ.items():
        if key.startswith(LOGIN_PREFIX) and value.strip():
            user = key[len(LOGIN_PREFIX) :].lower()
            creds[user] = value.strip()
    return creds


def verify_login(username: str, password: str, credentials: dict[str, str]) -> bool:
    user_key = username.strip().lower()
    expected = credentials.get(user_key)
    if not expected:
        return False
    return expected == password


def _parse_env_credentials(path: Path) -> dict[str, str]:
    creds: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith(LOGIN_PREFIX) and value:
            user = key[len(LOGIN_PREFIX) :].lower()
            creds[user] = value
    return creds

