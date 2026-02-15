from __future__ import annotations

import json
import os
import re
from datetime import datetime, UTC

from cashlyctl.config import LOGS_DIR, ensure_state_layout


TOKEN_PATTERNS = (
    re.compile(r"(?i)(token\s+)(\S+)"),
    re.compile(r"(?i)(password\s+)(\S+)"),
)
LOGON_PATTERN = re.compile(r"(?i)^(LOGON|L)\s+(\S+)\s+(\S+)\s*$")


def audit_command(profile_name: str, command_text: str) -> None:
    ensure_state_layout()
    log_path = LOGS_DIR / "commands.log"
    entry = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "profile": profile_name,
        "user": _current_user(),
        "command": _redact(command_text),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")))
        fh.write("\n")


def _redact(command_text: str) -> str:
    value = command_text.strip()
    logon_match = LOGON_PATTERN.match(value)
    if logon_match:
        return f"{logon_match.group(1)} {logon_match.group(2)} [REDACTED]"
    for pattern in TOKEN_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value


def _current_user() -> str:
    return os.getenv("USERNAME") or os.getenv("USER") or "unknown"
