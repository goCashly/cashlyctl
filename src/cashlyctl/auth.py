from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import string

from cashlyctl.config import APP_HOME
from cashlyctl.runtime_env import load_runtime_env_file, runtime_env


LOGIN_PREFIX = "CASHLYCTL_LOGIN_"
LOCAL_AUTH_DIR = APP_HOME / "auth"
LOCAL_USERS_PATH = LOCAL_AUTH_DIR / "local_users.json"
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8


class LocalAuthError(ValueError):
    """Raised when local user auth storage cannot accept an operation."""


@dataclass(frozen=True, slots=True)
class LocalUser:
    username: str
    role: str
    salt: str
    password_hash: str
    algorithm: str
    iterations: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class LocalAuthResult:
    username: str
    role: str


def load_login_credentials(env_file: str | None = None) -> dict[str, str]:
    creds: dict[str, str] = {}
    if env_file:
        env_path = Path(env_file)
        if env_path.exists():
            creds.update(_parse_env_credentials(env_path))
    else:
        creds.update(_parse_env_credentials_from_data(load_runtime_env_file()))

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


def has_local_users() -> bool:
    return bool(load_local_users())


def local_user_count() -> int:
    return len(load_local_users())


def load_local_users() -> dict[str, LocalUser]:
    raw = _read_local_users_payload()
    users_raw = raw.get("users")
    if not isinstance(users_raw, list):
        return {}

    users: dict[str, LocalUser] = {}
    for item in users_raw:
        if not isinstance(item, dict):
            continue
        try:
            user = LocalUser(
                username=str(item["username"]).strip().lower(),
                role=str(item.get("role", "admin")).strip().lower(),
                salt=str(item["salt"]),
                password_hash=str(item["password_hash"]),
                algorithm=str(item.get("algorithm", PASSWORD_HASH_ALGORITHM)),
                iterations=int(item.get("iterations", PASSWORD_HASH_ITERATIONS)),
                created_at=str(item.get("created_at", "")),
                updated_at=str(item.get("updated_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if _valid_username(user.username) and user.role in {"admin", "superadmin"}:
            users[user.username] = user
    return users


def create_local_user(username: str, password: str, role: str = "admin") -> LocalUser:
    user = username.strip().lower()
    normalized_role = role.strip().lower()
    if not _valid_username(user):
        raise LocalAuthError(
            "Username must be 3-32 characters using letters, numbers, dot, dash, or underscore."
        )
    if normalized_role not in {"admin", "superadmin"}:
        raise LocalAuthError("Role must be admin or superadmin.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise LocalAuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    users = load_local_users()
    if user in users:
        raise LocalAuthError(f"Local user already exists: {user}")

    now = datetime.now(tz=UTC).isoformat()
    salt = secrets.token_bytes(16)
    password_hash = _hash_password(password, salt, PASSWORD_HASH_ITERATIONS)
    local_user = LocalUser(
        username=user,
        role=normalized_role,
        salt=base64.b64encode(salt).decode("ascii"),
        password_hash=base64.b64encode(password_hash).decode("ascii"),
        algorithm=PASSWORD_HASH_ALGORITHM,
        iterations=PASSWORD_HASH_ITERATIONS,
        created_at=now,
        updated_at=now,
    )

    users_list = [
        _local_user_to_dict(existing)
        for existing in sorted(users.values(), key=lambda item: item.username)
    ]
    users_list.append(_local_user_to_dict(local_user))
    payload = {
        "version": 1,
        "users": sorted(users_list, key=lambda item: str(item["username"])),
    }
    _write_local_users_payload(payload)
    return local_user


def create_initial_admin_user(username: str, password: str) -> LocalUser:
    if has_local_users():
        raise LocalAuthError("Local users already exist. Initial admin bootstrap is disabled.")
    return create_local_user(username=username, password=password, role="admin")


def verify_local_login(username: str, password: str) -> LocalAuthResult | None:
    user = username.strip().lower()
    local_user = load_local_users().get(user)
    if not local_user:
        return None
    if local_user.algorithm != PASSWORD_HASH_ALGORITHM:
        return None
    try:
        salt = base64.b64decode(local_user.salt.encode("ascii"), validate=True)
        expected_hash = base64.b64decode(
            local_user.password_hash.encode("ascii"),
            validate=True,
        )
    except (ValueError, TypeError):
        return None
    actual_hash = _hash_password(password, salt, local_user.iterations)
    if not hmac.compare_digest(actual_hash, expected_hash):
        return None
    return LocalAuthResult(username=local_user.username, role=local_user.role)


def role_for_login_user(username: str) -> str:
    user = username.strip().lower()
    env_key = f"CASHLYCTL_ROLE_{user.upper()}"
    env_role = runtime_env(env_key, "").strip().lower()
    if env_role in {"admin", "superadmin"}:
        return env_role
    if user == "superadmin":
        return "superadmin"
    return "admin"


def _parse_env_credentials(path: Path) -> dict[str, str]:
    try:
        env_data = _parse_env_file(path)
    except OSError:
        return {}
    return _parse_env_credentials_from_data(env_data)


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            data[key] = value
    return data


def _parse_env_credentials_from_data(env_data: dict[str, str]) -> dict[str, str]:
    creds: dict[str, str] = {}
    for key, value in env_data.items():
        if key.startswith(LOGIN_PREFIX) and value.strip():
            user = key[len(LOGIN_PREFIX) :].lower()
            creds[user] = value.strip()
    return creds


def _read_local_users_payload() -> dict[str, object]:
    if not LOCAL_USERS_PATH.exists():
        return {"version": 1, "users": []}
    try:
        payload = json.loads(LOCAL_USERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "users": []}
    return payload if isinstance(payload, dict) else {"version": 1, "users": []}


def _write_local_users_payload(payload: dict[str, object]) -> None:
    LOCAL_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCAL_AUTH_DIR.chmod(0o700)
    except OSError:
        pass
    LOCAL_USERS_PATH.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        LOCAL_USERS_PATH.chmod(0o600)
    except OSError:
        pass


def _local_user_to_dict(user: LocalUser) -> dict[str, object]:
    return {
        "username": user.username,
        "role": user.role,
        "salt": user.salt,
        "password_hash": user.password_hash,
        "algorithm": user.algorithm,
        "iterations": user.iterations,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        max(100_000, iterations),
    )


def _valid_username(username: str) -> bool:
    if not 3 <= len(username) <= 32:
        return False
    allowed = set(string.ascii_lowercase + string.digits + "._-")
    return all(char in allowed for char in username)
