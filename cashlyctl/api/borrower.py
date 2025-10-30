# cashlyctl/api/borrower.py
"""
Thin client helpers for Borrower endpoints.
Currently supports *create* only, posting to `/v1/submit`.

The helper relies on :func:`cashlyctl.config.make_session`, which now loads
credentials from `.env`/environment variables and attaches `X-API-KEY`
automatically when ``CASHLY_API_KEY`` is present.
"""

from __future__ import annotations

from uuid import uuid4
from typing import Any, Dict

import requests

from ..config import endpoint, make_session

# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def create(payload: Dict[str, Any], *, session: requests.Session | None = None) -> Dict[str, Any]:
    """
    Insert a new Borrower via POST /v1/submit.

    Parameters
    ----------
    payload : dict
        Borrower data in the shape expected by the backend.
        If the backend does NOT auto-generate `IDBorrower`,
        we inject a UUID4 hex field automatically.
    session : requests.Session | None
        Re-use an existing session, or we’ll build one on the fly
        (complete with Accept + optional X-API-KEY headers).

    Returns
    -------
    dict
        The backend’s JSON response (usually containing the generated Cypher).
    """
    if session is None:
        session = make_session()

    # Safety: ensure an ID exists if the backend expects one.
    payload = dict(payload)  # copy to avoid mutating caller’s dict
    payload.setdefault("IDBorrower", uuid4().hex)  # harmless if backend ignores it

    url = endpoint("/v1/submit")
    response = session.post(url, json=payload, timeout=15)

    # Raise HTTP errors early so CLI can surface them nicely
    response.raise_for_status()
    return response.json()
