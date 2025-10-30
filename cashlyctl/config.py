"""
cashlyctl.config
----------------
Centralises run-time configuration: API base-URL, HTTP session setup,
and a tiny helper to build full endpoint URLs.

Environment variables
~~~~~~~~~~~~~~~~~~~~~
CASHLY_API_URL   Root URL for the Cashly API
                 (default: EC2 instance at port 8000)
CASHLY_API_KEY   Optional API key included as `X-API-KEY` header

You can override it like:
    $ CASHLY_API_URL="http://localhost:8000" cashlyctl borrower create ...
Or create a `.env` file alongside the CLI binary with values such as:
    CASHLY_API_URL=http://localhost:8000
    CASHLY_API_KEY=your-api-key
"""

from __future__ import annotations

import os
from typing import Optional

import requests
from dotenv import load_dotenv
from requests import Session

# Load environment variables from a local .env if present before
# any configuration helpers access them.
load_dotenv()

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_API_URL = "http://ec2-18-191-189-128.us-east-2.compute.amazonaws.com:8000"

# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #

def get_api_url() -> str:
    """
    Return the root URL that all CLI calls should hit.

    Priority:
    1. CASHLY_API_URL env var (trailing slash trimmed)
    2. DEFAULT_API_URL
    """
    return os.getenv("CASHLY_API_URL", DEFAULT_API_URL).rstrip("/")


def make_session(username: Optional[str] = None, password: Optional[str] = None) -> Session:
    """
    Create a `requests.Session` primed for JSON calls.

    • Sets an `Accept: application/json` header.
    • Injects `X-API-KEY` header automatically when CASHLY_API_KEY is set.
    • If `username` & `password` provided, attaches Basic-Auth credentials.
      (Currently optional because the backend is auth-free.)
    """
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    api_key = os.getenv("CASHLY_API_KEY")
    if api_key:
        session.headers["X-API-KEY"] = api_key

    if username and password:
        session.auth = (username, password)  # Basic Auth tuple
    return session


def endpoint(path: str) -> str:
    """
    Join the root API URL with an endpoint path.

    Example:
        url = endpoint("/v1/submit")   # → "http://.../v1/submit"
    """
    return f"{get_api_url()}/{path.lstrip('/')}"
