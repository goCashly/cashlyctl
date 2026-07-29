from __future__ import annotations

import os
from pathlib import Path


def default_app_home() -> Path:
    override = os.getenv("CASHLYCTL_HOME", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        app_data = os.getenv("APPDATA", "").strip()
        root = local_app_data or app_data
        if root:
            return Path(root) / "CashlyCTL"

    return Path.home() / ".cashlyctl"
