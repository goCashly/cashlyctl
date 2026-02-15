from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class DeploySpec:
    target: str
    host: str
    user: str
    port: int = 22
    ssh_key_path: str = ""
    app_dir: str = "/srv/app"
    pm2_process: str = "app"
    health_local: str = "http://127.0.0.1/health"
    health_public: str = ""
    default_ref: str = "origin/main"
    allow_nginx_reload: bool = False
    last_good_ref: str = ""


@dataclass(slots=True)
class DeployStepResult:
    name: str
    status: str
    detail: str


@dataclass(slots=True)
class DeployRunResult:
    action: str
    target: str
    status: str
    started_at: str
    finished_at: str
    ref: str
    steps: list[DeployStepResult] = field(default_factory=list)


@dataclass(slots=True)
class DeployReadinessResult:
    target: str
    status: str
    checked_at: str
    checks: list[DeployStepResult] = field(default_factory=list)


def load_deploy_specs(target_names: list[str]) -> dict[str, DeploySpec]:
    specs: dict[str, DeploySpec] = {}
    for target in target_names:
        key = _target_env_key(target)
        host = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_HOST", "").strip()
        user = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_USER", "").strip()
        if not host or not user:
            continue
        port_raw = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_PORT", "22").strip()
        try:
            port = int(port_raw)
        except ValueError:
            port = 22
        ssh_key_path = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_SSH_KEY_PATH", "").strip()
        if not ssh_key_path:
            # Backward-compatible alias for teams that prefer PEM naming.
            ssh_key_path = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_PEM_PATH", "").strip()
        if ssh_key_path:
            ssh_key_path = str(Path(ssh_key_path).expanduser())
        app_dir = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_APP_DIR", "/srv/app").strip() or "/srv/app"
        pm2_process = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_PM2_PROCESS", target).strip() or target
        health_local = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_HEALTH_LOCAL", "http://127.0.0.1/health").strip()
        health_public = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_HEALTH_PUBLIC", "").strip()
        default_ref = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_DEFAULT_REF", "origin/main").strip() or "origin/main"
        allow_nginx = (
            _runtime_env(f"CASHLYCTL_DEPLOY_{key}_NGINX_RELOAD", "0").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        last_good_ref = _runtime_env(f"CASHLYCTL_DEPLOY_{key}_LAST_GOOD", "").strip()
        specs[target.lower()] = DeploySpec(
            target=target,
            host=host,
            user=user,
            port=max(1, port),
            ssh_key_path=ssh_key_path,
            app_dir=app_dir,
            pm2_process=pm2_process,
            health_local=health_local,
            health_public=health_public,
            default_ref=default_ref,
            allow_nginx_reload=allow_nginx,
            last_good_ref=last_good_ref,
        )
    return specs


def probe_deploy_readiness(
    target_names: list[str],
    specs: dict[str, DeploySpec],
    timeout_sec: int = 10,
) -> dict[str, DeployReadinessResult]:
    ordered_targets: list[str] = []
    seen: set[str] = set()
    for target in target_names:
        key = target.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered_targets.append(target)

    if not ordered_targets:
        return {}

    results: dict[str, DeployReadinessResult] = {}
    max_workers = min(6, max(1, len(ordered_targets)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                probe_deploy_target_readiness,
                target,
                specs.get(target.lower()),
                timeout_sec,
            ): target
            for target in ordered_targets
        }
        for future in as_completed(futures):
            target = futures[future]
            key = target.lower()
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = DeployReadinessResult(
                    target=target,
                    status="FAIL",
                    checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    checks=[
                        DeployStepResult(
                            name="READINESS",
                            status="FAIL",
                            detail=f"Readiness probe crashed: {exc}",
                        )
                    ],
                )
    return results


def probe_deploy_target_readiness(
    target: str,
    spec: DeploySpec | None,
    timeout_sec: int = 10,
) -> DeployReadinessResult:
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checks: list[DeployStepResult] = []

    if not spec:
        checks.append(
            DeployStepResult(
                name="CONFIG",
                status="FAIL",
                detail="Missing deploy SSH config (host/user)",
            )
        )
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )

    checks.append(
        DeployStepResult(
            name="CONFIG",
            status="OK",
            detail=f"{spec.user}@{spec.host}:{spec.port} dir={spec.app_dir} pm2={spec.pm2_process}",
        )
    )

    if spec.ssh_key_path:
        if Path(spec.ssh_key_path).exists():
            checks.append(
                DeployStepResult(
                    name="SSH_KEY",
                    status="OK",
                    detail=spec.ssh_key_path,
                )
            )
        else:
            checks.append(
                DeployStepResult(
                    name="SSH_KEY",
                    status="FAIL",
                    detail=f"Key file not found: {spec.ssh_key_path}",
                )
            )
    else:
        checks.append(
            DeployStepResult(
                name="SSH_KEY",
                status="WARN",
                detail="No SSH key path set; relying on ssh-agent/default keys",
            )
        )

    if spec.health_local:
        checks.append(
            DeployStepResult(
                name="HEALTH_LOCAL",
                status="OK",
                detail=spec.health_local,
            )
        )
    else:
        checks.append(
            DeployStepResult(
                name="HEALTH_LOCAL",
                status="WARN",
                detail="Not configured",
            )
        )

    if spec.health_public:
        checks.append(
            DeployStepResult(
                name="HEALTH_PUBLIC",
                status="OK",
                detail=spec.health_public,
            )
        )
    else:
        checks.append(
            DeployStepResult(
                name="HEALTH_PUBLIC",
                status="WARN",
                detail="Not configured",
            )
        )

    if any(step.status == "FAIL" for step in checks):
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )

    if not _readiness_remote_check(
        checks,
        spec,
        "SSH_CONNECT",
        "echo cashlyctl-ready",
        timeout_sec,
        required=True,
        ok_detail="SSH auth/connection ready",
    ):
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )
    if not _readiness_remote_check(
        checks,
        spec,
        "APP_DIR",
        f"test -d {q(spec.app_dir)}",
        timeout_sec,
        required=True,
    ):
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )
    if not _readiness_remote_check(
        checks,
        spec,
        "PM2",
        "pm2 ping",
        timeout_sec,
        required=True,
    ):
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )
    if not _readiness_remote_check(
        checks,
        spec,
        "GIT_REMOTE",
        f"cd {q(spec.app_dir)} && git ls-remote --heads origin",
        timeout_sec,
        required=True,
        ok_detail="Git origin reachable",
    ):
        return DeployReadinessResult(
            target=target,
            status="FAIL",
            checked_at=checked_at,
            checks=checks,
        )

    _readiness_remote_check(
        checks,
        spec,
        "NODE",
        "node -v",
        timeout_sec,
        required=False,
    )
    _readiness_remote_check(
        checks,
        spec,
        "NPM",
        "npm -v",
        timeout_sec,
        required=False,
    )

    return DeployReadinessResult(
        target=target,
        status=_aggregate_status(checks),
        checked_at=checked_at,
        checks=checks,
    )


def run_deploy_via_ssh(
    spec: DeploySpec,
    revision: str = "",
    tag: str = "",
    timeout_sec: int = 120,
) -> DeployRunResult:
    ref = revision.strip() or (tag.strip() and f"tag:{tag.strip()}") or spec.default_ref
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps: list[DeployStepResult] = []

    ok, detail = _preflight(spec, timeout_sec)
    steps.extend(detail)
    if not ok:
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)

    if not _step_exec(steps, spec, "FETCH", f"cd {q(spec.app_dir)} && git fetch --all", timeout_sec):
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)

    checkout_ref = revision.strip() or (f"tags/{tag.strip()}" if tag.strip() else spec.default_ref)
    if not _step_exec(
        steps,
        spec,
        "CHECKOUT",
        f"cd {q(spec.app_dir)} && git checkout {q(checkout_ref)}",
        timeout_sec,
    ):
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)

    if not _step_exec(steps, spec, "NPM_CI", f"cd {q(spec.app_dir)} && npm ci", timeout_sec):
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)
    if not _step_exec(steps, spec, "BUILD", f"cd {q(spec.app_dir)} && npm run build", timeout_sec):
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)

    if not _reload_pm2(steps, spec, timeout_sec):
        return _finish("DEPLOY", spec.target, "FAIL", started, ref, steps)

    if spec.allow_nginx_reload:
        _step_exec(steps, spec, "NGINX_TEST", "nginx -t", timeout_sec, required=False)
        _step_exec(steps, spec, "NGINX_RELOAD", "nginx -s reload", timeout_sec, required=False)

    _verify_health(steps, spec, timeout_sec)
    status = _aggregate_status(steps)
    return _finish("DEPLOY", spec.target, status, started, ref, steps)


def run_rollback_via_ssh(
    spec: DeploySpec,
    to_ref: str,
    timeout_sec: int = 120,
) -> DeployRunResult:
    rollback_ref = to_ref.strip() or spec.last_good_ref.strip()
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps: list[DeployStepResult] = []

    if rollback_ref.lower() == "last-good":
        rollback_ref = spec.last_good_ref.strip()
    if not rollback_ref:
        steps.append(
            DeployStepResult(
                name="ROLLBACK_REF",
                status="FAIL",
                detail="Missing rollback reference; set CASHLYCTL_DEPLOY_<TARGET>_LAST_GOOD or provide explicit --to",
            )
        )
        return _finish("ROLLBACK", spec.target, "FAIL", started, "last-good", steps)

    ok, detail = _preflight(spec, timeout_sec)
    steps.extend(detail)
    if not ok:
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)

    if not _step_exec(steps, spec, "FETCH", f"cd {q(spec.app_dir)} && git fetch --all", timeout_sec):
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)
    if not _step_exec(
        steps,
        spec,
        "CHECKOUT",
        f"cd {q(spec.app_dir)} && git checkout {q(rollback_ref)}",
        timeout_sec,
    ):
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)

    if not _step_exec(steps, spec, "NPM_CI", f"cd {q(spec.app_dir)} && npm ci", timeout_sec):
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)
    if not _step_exec(steps, spec, "BUILD", f"cd {q(spec.app_dir)} && npm run build", timeout_sec):
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)

    if not _reload_pm2(steps, spec, timeout_sec):
        return _finish("ROLLBACK", spec.target, "FAIL", started, rollback_ref, steps)

    _verify_health(steps, spec, timeout_sec)
    status = _aggregate_status(steps)
    return _finish("ROLLBACK", spec.target, status, started, rollback_ref, steps)


def _preflight(spec: DeploySpec, timeout_sec: int) -> tuple[bool, list[DeployStepResult]]:
    steps: list[DeployStepResult] = []
    if not spec.host or not spec.user:
        steps.append(DeployStepResult("CONFIG", "FAIL", "Missing SSH host/user config"))
        return False, steps
    if spec.ssh_key_path and not Path(spec.ssh_key_path).exists():
        steps.append(
            DeployStepResult(
                "CONFIG",
                "FAIL",
                f"SSH key not found: {spec.ssh_key_path}",
            )
        )
        return False, steps
    if not _step_exec(steps, spec, "PREFLIGHT_DIR", f"test -d {q(spec.app_dir)}", timeout_sec):
        return False, steps
    _step_exec(
        steps,
        spec,
        "PREFLIGHT_GIT_REMOTE",
        f"cd {q(spec.app_dir)} && git remote -v",
        timeout_sec,
        required=False,
    )
    if not _step_exec(steps, spec, "PREFLIGHT_DISK", f"df -h {q(spec.app_dir)}", timeout_sec):
        return False, steps
    if not _step_exec(steps, spec, "PREFLIGHT_PM2", "pm2 ping", timeout_sec):
        return False, steps
    _step_exec(steps, spec, "PREFLIGHT_PORTS", "ss -ltn | head -n 20", timeout_sec, required=False)
    return True, steps


def _readiness_remote_check(
    checks: list[DeployStepResult],
    spec: DeploySpec,
    check_name: str,
    command: str,
    timeout_sec: int,
    required: bool = True,
    ok_detail: str = "",
) -> bool:
    ok, out = _ssh_exec(spec, command, timeout_sec)
    if ok:
        checks.append(DeployStepResult(check_name, "OK", ok_detail or out))
        return True
    status = "FAIL" if required else "WARN"
    checks.append(DeployStepResult(check_name, status, out))
    return not required


def _reload_pm2(steps: list[DeployStepResult], spec: DeploySpec, timeout_sec: int) -> bool:
    if _step_exec(
        steps,
        spec,
        "PM2_RELOAD",
        f"pm2 reload {q(spec.pm2_process)}",
        timeout_sec,
        required=False,
    ):
        return True
    return _step_exec(
        steps,
        spec,
        "PM2_RESTART",
        f"pm2 restart {q(spec.pm2_process)}",
        timeout_sec,
    )


def _verify_health(steps: list[DeployStepResult], spec: DeploySpec, timeout_sec: int) -> None:
    if spec.health_local:
        _step_exec(
            steps,
            spec,
            "VERIFY_LOCAL_HEALTH",
            f"curl -fsS -m 10 {q(spec.health_local)}",
            timeout_sec,
            required=False,
        )
    if spec.health_public:
        _step_exec(
            steps,
            spec,
            "VERIFY_PUBLIC_HEALTH",
            f"curl -fsS -m 10 {q(spec.health_public)}",
            timeout_sec,
            required=False,
        )


def _step_exec(
    steps: list[DeployStepResult],
    spec: DeploySpec,
    step_name: str,
    command: str,
    timeout_sec: int,
    required: bool = True,
) -> bool:
    ok, out = _ssh_exec(spec, command, timeout_sec)
    if ok:
        steps.append(DeployStepResult(step_name, "OK", out))
        return True
    status = "FAIL" if required else "WARN"
    steps.append(DeployStepResult(step_name, status, out))
    return not required


def _ssh_exec(spec: DeploySpec, remote_command: str, timeout_sec: int) -> tuple[bool, str]:
    ssh_cmd = [
        "ssh",
        "-p",
        str(spec.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if spec.ssh_key_path:
        ssh_cmd.extend(["-i", spec.ssh_key_path])
    ssh_cmd.extend([f"{spec.user}@{spec.host}", remote_command])
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=max(10, timeout_sec),
            check=False,
        )
    except FileNotFoundError:
        return False, "ssh binary not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "ssh command timed out"
    except Exception as exc:
        return False, f"ssh command failed: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined = stdout
    if stderr:
        combined = f"{stdout}\n{stderr}".strip()
    detail = combined or f"exit={result.returncode}"
    return result.returncode == 0, detail


def _aggregate_status(steps: list[DeployStepResult]) -> str:
    if any(step.status == "FAIL" for step in steps):
        return "FAIL"
    if any(step.status == "WARN" for step in steps):
        return "WARN"
    return "OK"


def _finish(
    action: str,
    target: str,
    status: str,
    started_at: str,
    ref: str,
    steps: list[DeployStepResult],
) -> DeployRunResult:
    return DeployRunResult(
        action=action,
        target=target,
        status=status,
        started_at=started_at,
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ref=ref,
        steps=steps,
    )


def q(value: str) -> str:
    return shlex.quote(value)


def _target_env_key(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name).upper()


_RUNTIME_ENV_CACHE: dict[str, str] | None = None


def _runtime_env(key: str, default: str) -> str:
    value = os.getenv(key, "").strip()
    if value:
        return value
    env_data = _load_runtime_env_file()
    file_value = env_data.get(key, "").strip()
    if file_value:
        return file_value
    return default


def _load_runtime_env_file(path: str = ".env") -> dict[str, str]:
    global _RUNTIME_ENV_CACHE
    if _RUNTIME_ENV_CACHE is not None:
        return _RUNTIME_ENV_CACHE

    data: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        _RUNTIME_ENV_CACHE = data
        return data

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        _RUNTIME_ENV_CACHE = data
        return data

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        key_clean = env_key.strip()
        value_clean = value.strip().strip('"').strip("'")
        if key_clean and value_clean:
            data[key_clean] = value_clean

    _RUNTIME_ENV_CACHE = data
    return data
