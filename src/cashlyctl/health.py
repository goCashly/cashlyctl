from __future__ import annotations

from datetime import datetime, UTC
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cashlyctl.models import DeploymentMode, HealthCheckResult, HealthStatus, Profile


def run_mvp_checks(profile: Profile) -> list[HealthCheckResult]:
    now = datetime.now(tz=UTC)
    checks: list[HealthCheckResult] = []

    if profile.mode == DeploymentMode.ENTERPRISE:
        checks.append(_probe_http("control_api", profile.control_api_base_url, required=True, now=now))
    elif profile.control_api_base_url:
        checks.append(_probe_http("control_api", profile.control_api_base_url, required=False, now=now))

    if profile.mode == DeploymentMode.INTERNAL and profile.neo4j_bolt_uri:
        checks.append(
            _probe_tcp("neo4j", profile.neo4j_bolt_uri, default_port=7687, required=False, now=now)
        )
    if profile.mode == DeploymentMode.INTERNAL and profile.dealsense_url:
        checks.append(_probe_http("dealsense", profile.dealsense_url, required=False, now=now))

    return checks


def _probe_http(name: str, base_url: str | None, required: bool, now: datetime) -> HealthCheckResult:
    target = _health_url(base_url or "")
    if not target:
        status = HealthStatus.FAIL if required else HealthStatus.WARN
        detail = "missing required endpoint" if required else "not configured"
        return HealthCheckResult(
            name=name,
            status=status,
            latency_ms=0,
            detail=detail,
            timestamp=now,
        )

    request = urllib.request.Request(
        target,
        headers={"User-Agent": "cashlyctl/0.1"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            status_code = int(getattr(response, "status", 200))
            if 200 <= status_code < 400:
                status = HealthStatus.OK
            else:
                status = HealthStatus.WARN
            return HealthCheckResult(
                name=name,
                status=status,
                latency_ms=elapsed_ms,
                detail=f"http {status_code} ({target})",
                timestamp=now,
            )
    except urllib.error.HTTPError as exc:
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        status_code = int(getattr(exc, "code", 0) or 0)
        status = HealthStatus.WARN if status_code else HealthStatus.FAIL
        return HealthCheckResult(
            name=name,
            status=status,
            latency_ms=elapsed_ms,
            detail=f"http {status_code} ({target})",
            timestamp=now,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        return HealthCheckResult(
            name=name,
            status=HealthStatus.FAIL,
            latency_ms=elapsed_ms,
            detail=f"unreachable ({target}) [{exc}]",
            timestamp=now,
        )


def _probe_tcp(
    name: str,
    endpoint: str | None,
    default_port: int,
    required: bool,
    now: datetime,
) -> HealthCheckResult:
    raw = (endpoint or "").strip()
    if not raw:
        status = HealthStatus.FAIL if required else HealthStatus.WARN
        detail = "missing required endpoint" if required else "not configured"
        return HealthCheckResult(
            name=name,
            status=status,
            latency_ms=0,
            detail=detail,
            timestamp=now,
        )

    parsed = urlparse(raw if "://" in raw else f"tcp://{raw}")
    host = parsed.hostname
    port = parsed.port or default_port
    if not host:
        return HealthCheckResult(
            name=name,
            status=HealthStatus.FAIL,
            latency_ms=0,
            detail=f"invalid endpoint ({raw})",
            timestamp=now,
        )

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3):
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return HealthCheckResult(
                name=name,
                status=HealthStatus.OK,
                latency_ms=elapsed_ms,
                detail=f"tcp {host}:{port} reachable",
                timestamp=now,
            )
    except OSError as exc:
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        return HealthCheckResult(
            name=name,
            status=HealthStatus.FAIL,
            latency_ms=elapsed_ms,
            detail=f"tcp {host}:{port} unreachable [{exc}]",
            timestamp=now,
        )


def _health_url(base_url: str) -> str:
    value = base_url.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{value}")
    if not parsed.netloc:
        return ""
    path = parsed.path or ""
    if not path or path == "/":
        path = "/health"
    return parsed._replace(path=path, query="", fragment="").geturl()
