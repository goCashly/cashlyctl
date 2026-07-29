from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse


class ProbeOverall(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(slots=True)
class NetworkProbeTarget:
    name: str
    url: str


@dataclass(slots=True)
class NetworkProbeResult:
    name: str
    url: str
    dns_ok: bool
    dns_ms: int
    dns_ip: str
    tls_ok: bool
    tls_ms: int
    tls_days_left: int
    tls_cn: str
    http_ok: bool
    http_ms: int
    http_status: int
    http_hint: str
    overall: ProbeOverall


def probe_targets(
    targets: list[NetworkProbeTarget],
    timeout_seconds: float = 3.0,
    latency_warn_ms: int = 600,
    tls_warn_days: int = 14,
) -> list[NetworkProbeResult]:
    if not targets:
        return []

    order = {target.name: idx for idx, target in enumerate(targets)}
    worker_count = max(1, min(len(targets), 8))
    results: list[NetworkProbeResult] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cashly-probe") as pool:
        futures = {
            pool.submit(
                probe_target,
                target,
                timeout_seconds=timeout_seconds,
                latency_warn_ms=latency_warn_ms,
                tls_warn_days=tls_warn_days,
            ): target
            for target in targets
        }
        for future in as_completed(futures):
            target = futures[future]
            try:
                result = future.result()
            except Exception:
                result = NetworkProbeResult(
                    name=target.name,
                    url=target.url,
                    dns_ok=False,
                    dns_ms=0,
                    dns_ip="-",
                    tls_ok=False,
                    tls_ms=0,
                    tls_days_left=0,
                    tls_cn="",
                    http_ok=False,
                    http_ms=0,
                    http_status=0,
                    http_hint="probe_error",
                    overall=ProbeOverall.FAIL,
                )
            results.append(result)
    results.sort(key=lambda item: order.get(item.name, 10_000))
    return results


def probe_target(
    target: NetworkProbeTarget,
    timeout_seconds: float = 3.0,
    latency_warn_ms: int = 600,
    tls_warn_days: int = 14,
) -> NetworkProbeResult:
    parsed = urlparse(target.url)
    host = parsed.hostname or ""
    scheme = (parsed.scheme or "https").lower()
    port = parsed.port or (443 if scheme == "https" else 80)

    dns_ok, dns_ms, dns_ip = _probe_dns(host, port, timeout_seconds)
    tls_ok, tls_ms, tls_days_left, tls_cn = _probe_tls(host, port, timeout_seconds, scheme)
    http_ok, http_ms, http_status, http_hint = _probe_http(target.url, timeout_seconds)

    overall = _compute_overall(
        dns_ok=dns_ok,
        tls_ok=tls_ok,
        dns_ms=dns_ms,
        tls_ms=tls_ms,
        http_ok=http_ok,
        http_ms=http_ms,
        http_status=http_status,
        tls_days_left=tls_days_left,
        latency_warn_ms=latency_warn_ms,
        tls_warn_days=tls_warn_days,
    )
    return NetworkProbeResult(
        name=target.name,
        url=target.url,
        dns_ok=dns_ok,
        dns_ms=dns_ms,
        dns_ip=dns_ip or "-",
        tls_ok=tls_ok,
        tls_ms=tls_ms,
        tls_days_left=max(0, tls_days_left),
        tls_cn=tls_cn,
        http_ok=http_ok,
        http_ms=http_ms,
        http_status=http_status,
        http_hint=http_hint,
        overall=overall,
    )


def _probe_dns(host: str, port: int, timeout_seconds: float) -> tuple[bool, int, str]:
    if not host:
        return False, 0, "-"
    start = time.perf_counter()
    try:
        entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False, _elapsed_ms(start), "-"

    ip = "-"
    for entry in entries:
        candidate = entry[4][0]
        if ":" not in candidate:
            ip = candidate
            break
        if ip == "-":
            ip = candidate
    return True, _elapsed_ms(start), ip


def _probe_tls(
    host: str,
    port: int,
    timeout_seconds: float,
    scheme: str,
) -> tuple[bool, int, int, str]:
    if scheme != "https" or not host:
        return True, 0, 0, ""

    start = time.perf_counter()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                cert = tls_sock.getpeercert()
    except OSError:
        return False, _elapsed_ms(start), 0, ""
    except ssl.SSLError:
        return False, _elapsed_ms(start), 0, ""

    days_left = _cert_days_left(cert)
    common_name = _cert_common_name(cert)
    return True, _elapsed_ms(start), days_left, common_name


def _probe_http(url: str, timeout_seconds: float) -> tuple[bool, int, int, str]:
    start = time.perf_counter()
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "cashlyctl/0.1"})
    opener = urllib.request.build_opener(_NoRedirectHandler())

    headers = None
    status = 0
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.getcode() or 0)
            headers = response.headers
    except urllib.error.HTTPError as err:
        status = int(err.code or 0)
        headers = err.headers
    except urllib.error.URLError as err:
        hint = "timeout" if _is_timeout_reason(err.reason) else "http_unreachable"
        return False, _elapsed_ms(start), 0, hint
    except TimeoutError:
        return False, _elapsed_ms(start), 0, "timeout"
    except OSError:
        return False, _elapsed_ms(start), 0, "http_unreachable"

    hint = _http_hint_for(status, headers)
    return True, _elapsed_ms(start), status, hint


def _compute_overall(
    *,
    dns_ok: bool,
    tls_ok: bool,
    dns_ms: int,
    tls_ms: int,
    http_ok: bool,
    http_ms: int,
    http_status: int,
    tls_days_left: int,
    latency_warn_ms: int,
    tls_warn_days: int,
) -> ProbeOverall:
    if not dns_ok or not tls_ok:
        return ProbeOverall.FAIL
    if not http_ok and http_status == 0:
        return ProbeOverall.FAIL

    http_warn = http_status in {401, 403, 429} or http_status >= 500
    tls_warn = tls_days_left > 0 and tls_days_left < tls_warn_days
    latency_warn = max(dns_ms, tls_ms, http_ms) > latency_warn_ms
    if http_warn or tls_warn or latency_warn:
        return ProbeOverall.WARN

    if 200 <= http_status <= 399:
        return ProbeOverall.OK
    return ProbeOverall.WARN


def _cert_days_left(cert: dict[str, object]) -> int:
    raw = cert.get("notAfter")
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        expires = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except ValueError:
        return 0
    delta = expires - datetime.now(tz=UTC)
    return max(0, int(delta.total_seconds() // 86400))


def _cert_common_name(cert: dict[str, object]) -> str:
    subject = cert.get("subject")
    if not isinstance(subject, tuple):
        return ""
    for part in subject:
        if not isinstance(part, tuple):
            continue
        for entry in part:
            if (
                isinstance(entry, tuple)
                and len(entry) == 2
                and str(entry[0]).lower() == "commonname"
            ):
                return str(entry[1])
    return ""


def _http_hint_for(status: int, headers: object) -> str:
    hints: list[str] = []
    server = ""
    cf_ray = ""
    location = ""
    if headers is not None:
        try:
            server = str(headers.get("server", "")).lower()
            cf_ray = str(headers.get("cf-ray", "")).strip()
            location = str(headers.get("location", "")).strip()
        except Exception:
            server = ""

    if "cloudflare" in server or cf_ray:
        hints.append("cloudflare")
    if 300 <= status <= 399 or location:
        hints.append("redirect")
    if status in {401, 403}:
        hints.append("auth")
    elif status == 429:
        hints.append("rate-limit")
    elif status >= 500:
        hints.append("upstream")

    if not hints:
        return "-"
    return " ".join(hints)


def _elapsed_ms(start_time: float) -> int:
    return int((time.perf_counter() - start_time) * 1000)


def _is_timeout_reason(reason: object) -> bool:
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(reason, socket.timeout):
        return True
    text = str(reason).lower()
    return "timed out" in text or "timeout" in text


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None
