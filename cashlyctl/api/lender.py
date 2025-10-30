# cashlyctl/api/lender.py
"""Helper utilities for uploading lender payloads to the Cashly API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping

import requests

from ..config import endpoint, make_session

JsonMapping = Mapping[str, object]
MutableJsonMapping = MutableMapping[str, object]
PathLike = str | Path


def submit_payloads(
    payloads: Iterable[JsonMapping | MutableJsonMapping],
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> list[dict[str, object]]:
    """Submit an iterable of JSON payloads to the ``/v1/submit`` endpoint.

    Parameters
    ----------
    payloads:
        Any iterable producing dictionaries compatible with ``requests``'s
        ``json`` parameter.
    session:
        Optional reusable :class:`requests.Session`.  If omitted a new session
        is created via :func:`cashlyctl.config.make_session`.
    timeout:
        Per-request timeout in seconds.

    Returns
    -------
    list[dict[str, object]]
        JSON responses returned by the backend for each payload in order.
    """

    session = session or make_session()
    url = endpoint("/v1/submit")
    responses: list[dict[str, object]] = []
    for payload in payloads:
        response = session.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        responses.append(response.json())
    return responses


def submit_files(
    paths: Iterable[PathLike],
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
    encoding: str = "utf-8",
) -> list[dict[str, object]]:
    """Submit JSON payloads loaded from ``paths`` to the API.

    Each path is read with :meth:`pathlib.Path.read_text` using ``encoding`` and
    parsed as JSON before delegating to :func:`submit_payloads`.
    """

    payloads: list[JsonMapping] = []
    for path in paths:
        data = Path(path).read_text(encoding=encoding)
        payloads.append(json.loads(data))
    return submit_payloads(payloads, session=session, timeout=timeout)


__all__ = ["submit_payloads", "submit_files"]
