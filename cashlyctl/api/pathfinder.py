# cashlyctl/api/pathfinder.py
"""API helper for the Pathfinder endpoint."""

from __future__ import annotations

from typing import Any, Dict

import requests

from ..config import endpoint, make_session

__all__ = ["run"]


def run(payload: Dict[str, Any], *, session: requests.Session | None = None) -> Dict[str, Any]:
    """Submit a Pathfinder request via POST /v1/pathfinder."""
    if session is None:
        session = make_session()

    url = endpoint("/v1/pathfinder")
    response = session.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()
