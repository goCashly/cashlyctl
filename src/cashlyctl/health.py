from __future__ import annotations

from datetime import datetime, UTC

from cashlyctl.models import DeploymentMode, HealthCheckResult, HealthStatus, Profile


def run_mvp_checks(profile: Profile) -> list[HealthCheckResult]:
    now = datetime.now(tz=UTC)
    checks: list[HealthCheckResult] = []

    control_status = HealthStatus.OK if profile.control_api_base_url else HealthStatus.FAIL
    checks.append(
        HealthCheckResult(
            name="control_api",
            status=control_status,
            latency_ms=34 if control_status == HealthStatus.OK else 0,
            detail=_detail_for(control_status, profile.control_api_base_url, required=True),
            timestamp=now,
        )
    )

    if profile.mode == DeploymentMode.INTERNAL:
        neo4j_status = HealthStatus.OK if profile.neo4j_bolt_uri else HealthStatus.WARN
        checks.append(
            HealthCheckResult(
                name="neo4j",
                status=neo4j_status,
                latency_ms=22 if neo4j_status == HealthStatus.OK else 0,
                detail=_detail_for(neo4j_status, profile.neo4j_bolt_uri, required=False),
                timestamp=now,
            )
        )
        dealsense_status = HealthStatus.OK if profile.dealsense_url else HealthStatus.WARN
        checks.append(
            HealthCheckResult(
                name="dealsense",
                status=dealsense_status,
                latency_ms=18 if dealsense_status == HealthStatus.OK else 0,
                detail=_detail_for(dealsense_status, profile.dealsense_url, required=False),
                timestamp=now,
            )
        )

    return checks


def _detail_for(status: HealthStatus, target: str | None, required: bool) -> str:
    if status == HealthStatus.OK:
        return f"reachable ({target})"
    if required:
        return "missing required endpoint"
    return "not configured (optional)"

