from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from urllib.parse import urlparse


VALID_HOST_OS = {"windows", "macos", "linux", "unknown"}
VALID_HOTKEY_BACKENDS = {
    "auto",
    "windows",
    "macos",
    "x11",
    "wayland_portal",
    "desktop_shortcut",
    "none",
}


@dataclass(slots=True)
class HostInspection:
    runtime_os: str
    runtime_arch: str
    host_os: str
    host_os_confidence: str
    session_type: str
    display_server: str
    is_container: bool
    container_runtime: str
    docker_cli: str
    docker_context: str
    is_wsl: bool
    docker_socket: str
    hotkey_support: str
    recommended_backend: str
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_host() -> HostInspection:
    runtime_os = _runtime_os()
    runtime_arch = platform.machine() or "unknown"
    is_wsl = _is_wsl()
    container_runtime = _container_runtime()
    is_container = container_runtime != "none"
    session_type = _session_type(runtime_os, is_wsl)
    display_server = _display_server(runtime_os)
    docker_cli = _docker_cli_state()
    docker_context = _docker_context_name()
    docker_socket = _docker_socket_state()
    host_os, host_os_confidence, host_notes = _host_os(runtime_os, is_wsl, is_container)
    hotkey_support, recommended_backend, hotkey_notes = _hotkey_guidance(
        runtime_os=runtime_os,
        host_os=host_os,
        session_type=session_type,
        display_server=display_server,
        is_container=is_container,
        is_wsl=is_wsl,
    )

    notes = [*host_notes, *hotkey_notes]
    notes.extend(_docker_notes(docker_cli, docker_socket))
    backend_override = _clean_env("CASHLYCTL_HOTKEY_BACKEND").lower()
    if backend_override:
        if backend_override in VALID_HOTKEY_BACKENDS:
            recommended_backend = backend_override
            notes.append(f"CASHLYCTL_HOTKEY_BACKEND override is set to {backend_override}.")
        else:
            notes.append(
                "Ignoring invalid CASHLYCTL_HOTKEY_BACKEND override "
                f"{backend_override!r}; expected one of {', '.join(sorted(VALID_HOTKEY_BACKENDS))}."
            )

    return HostInspection(
        runtime_os=runtime_os,
        runtime_arch=runtime_arch,
        host_os=host_os,
        host_os_confidence=host_os_confidence,
        session_type=session_type,
        display_server=display_server,
        is_container=is_container,
        container_runtime=container_runtime,
        docker_cli=docker_cli,
        docker_context=docker_context,
        is_wsl=is_wsl,
        docker_socket=docker_socket,
        hotkey_support=hotkey_support,
        recommended_backend=recommended_backend,
        notes=notes,
    )


def format_host_inspection(report: HostInspection) -> str:
    rows = [
        ("Runtime OS", report.runtime_os),
        ("Runtime arch", report.runtime_arch),
        ("Host OS", f"{report.host_os} ({report.host_os_confidence})"),
        ("Session type", report.session_type),
        ("Display server", report.display_server),
        ("Container", _yes_no(report.is_container)),
        ("Container runtime", report.container_runtime),
        ("Docker CLI", report.docker_cli),
        ("Docker context", report.docker_context),
        ("WSL", _yes_no(report.is_wsl)),
        ("Docker socket", report.docker_socket),
        ("Hotkey support", report.hotkey_support),
        ("Recommended backend", report.recommended_backend),
    ]
    width = max(len(label) for label, _ in rows)
    lines = ["cashlyctl host inspection"]
    lines.extend(f"{label:<{width}}  {value}" for label, value in rows)
    if report.notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines)


def _runtime_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system.startswith("win"):
        return "windows"
    if system == "linux":
        return "linux"
    if sys.platform.startswith("linux"):
        return "linux"
    return system or "unknown"


def _host_os(runtime_os: str, is_wsl: bool, is_container: bool) -> tuple[str, str, list[str]]:
    override = _clean_env("CASHLYCTL_HOST_OS").lower()
    if override:
        if override in VALID_HOST_OS:
            return override, "override", [f"CASHLYCTL_HOST_OS override is set to {override}."]
        return (
            "unknown",
            "low",
            [
                "Ignoring invalid CASHLYCTL_HOST_OS override "
                f"{override!r}; expected one of {', '.join(sorted(VALID_HOST_OS))}."
            ],
        )

    if is_wsl:
        return "windows", "medium", ["Detected WSL; host OS is assumed to be Windows."]

    if is_container:
        return (
            "unknown",
            "low",
            [
                "Detected a container runtime. Set CASHLYCTL_HOST_OS when host-aware behavior matters.",
            ],
        )

    if runtime_os in {"windows", "macos", "linux"}:
        return runtime_os, "high", []

    return "unknown", "low", ["Runtime OS is not recognized."]


def _hotkey_guidance(
    *,
    runtime_os: str,
    host_os: str,
    session_type: str,
    display_server: str,
    is_container: bool,
    is_wsl: bool,
) -> tuple[str, str, list[str]]:
    if is_container:
        return (
            "external_shortcut_only",
            "desktop_shortcut",
            ["Docker/container runtimes cannot reliably register host-level global hotkeys."],
        )

    if is_wsl:
        return (
            "external_shortcut_only",
            "windows_native_helper",
            ["WSL cannot reliably register Windows host hotkeys from inside Linux."],
        )

    if host_os == "windows":
        return "native_global_hotkeys", "windows", []

    if host_os == "macos":
        return (
            "native_with_permissions",
            "macos",
            ["macOS may require Accessibility/Input Monitoring approval for hotkey listeners."],
        )

    if host_os == "linux":
        if display_server == "x11":
            return "native_global_hotkeys", "x11", []
        if display_server == "wayland":
            return (
                "portal_or_external_shortcut",
                "wayland_portal",
                [
                    "Wayland restricts global keyboard capture; use portal support when available.",
                    "If portal support is unavailable, bind desktop shortcuts to cashlyctl crm commands.",
                ],
            )
        if session_type in {"ssh", "headless"}:
            return (
                "not_available",
                "none",
                ["No graphical desktop session detected; global hotkeys are not available."],
            )
        return (
            "desktop_session_unknown",
            "desktop_shortcut",
            ["Linux desktop session type is unknown; run from the graphical session or configure a desktop shortcut."],
        )

    if runtime_os in {"windows", "macos", "linux"}:
        return "unknown", "auto", ["Host OS could not be inferred with enough confidence."]

    return "not_available", "none", ["Unsupported runtime OS for native hotkeys."]


def _session_type(runtime_os: str, is_wsl: bool) -> str:
    if is_wsl:
        return "wsl"
    if runtime_os != "linux":
        return runtime_os
    xdg_session = _clean_env("XDG_SESSION_TYPE").lower()
    if xdg_session:
        return xdg_session
    if _clean_env("SSH_CONNECTION") or _clean_env("SSH_TTY"):
        return "ssh"
    if not _clean_env("DISPLAY") and not _clean_env("WAYLAND_DISPLAY"):
        return "headless"
    return "unknown"


def _display_server(runtime_os: str) -> str:
    if runtime_os != "linux":
        return "not_applicable"
    xdg_session = _clean_env("XDG_SESSION_TYPE").lower()
    if xdg_session in {"x11", "wayland"}:
        return xdg_session
    if _clean_env("WAYLAND_DISPLAY"):
        return "wayland"
    if _clean_env("DISPLAY"):
        return "x11"
    return "none"


def _is_wsl() -> bool:
    if _clean_env("WSL_DISTRO_NAME") or _clean_env("WSL_INTEROP"):
        return True
    for path in (Path("/proc/version"), Path("/proc/sys/kernel/osrelease")):
        text = _read_text(path).lower()
        if "microsoft" in text or "wsl" in text:
            return True
    return False


def _container_runtime() -> str:
    env_container = _clean_env("container").lower()
    if env_container:
        return env_container
    if Path("/.dockerenv").exists():
        return "docker"
    if Path("/run/.containerenv").exists():
        return "podman"
    cgroup = _read_text(Path("/proc/1/cgroup")).lower()
    markers = [
        ("docker", "docker"),
        ("kubepods", "kubernetes"),
        ("containerd", "containerd"),
        ("libpod", "podman"),
        ("podman", "podman"),
        ("lxc", "lxc"),
    ]
    for marker, label in markers:
        if marker in cgroup:
            return label
    if _clean_env("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    return "none"


def _docker_socket_state() -> str:
    docker_host = _clean_env("DOCKER_HOST")
    if docker_host:
        return _docker_host_state(docker_host)

    context_host = _docker_context_host()
    if context_host:
        context_state = _docker_host_state(context_host)
        if context_state != "missing":
            return context_state

    rootless_runtime_dir = _clean_env("XDG_RUNTIME_DIR")
    socket_candidates = []
    if rootless_runtime_dir:
        socket_candidates.append(Path(rootless_runtime_dir) / "docker.sock")
    try:
        socket_candidates.append(Path(f"/run/user/{os.getuid()}/docker.sock"))
    except OSError:
        pass
    socket_candidates.append(Path.home() / ".docker" / "desktop" / "docker.sock")
    socket_candidates.append(Path("/var/run/docker.sock"))

    for socket_path in socket_candidates:
        state = _socket_path_state(socket_path)
        if state != "missing":
            return state
    return "missing"


def _socket_path_state(socket_path: Path) -> str:
    if not socket_path.exists():
        return "missing"
    if os.access(socket_path, os.R_OK | os.W_OK):
        return f"available:{socket_path}"
    if os.access(socket_path, os.R_OK):
        return f"read_only:{socket_path}"
    if os.access(socket_path, os.W_OK):
        return f"write_only:{socket_path}"
    return f"permission_denied:{socket_path}"


def _docker_host_state(docker_host: str) -> str:
    parsed = urlparse(docker_host)
    if parsed.scheme in {"tcp", "http", "https", "ssh"}:
        return f"remote:{parsed.scheme}"
    if parsed.scheme == "unix":
        socket_path = Path(parsed.path)
        return _socket_path_state(socket_path)
    return "configured_unknown"


def _docker_cli_state() -> str:
    docker_path = shutil.which("docker")
    if docker_path:
        return f"available:{docker_path}"
    return "missing"


def _docker_context_name() -> str:
    config = _read_json(Path.home() / ".docker" / "config.json")
    current_context = config.get("currentContext")
    if isinstance(current_context, str) and current_context.strip():
        return current_context.strip()
    return "default"


def _docker_context_host() -> str:
    context_name = _docker_context_name()
    if context_name == "default":
        return ""

    meta_root = Path.home() / ".docker" / "contexts" / "meta"
    if not meta_root.exists():
        return ""

    for meta_path in sorted(meta_root.glob("*/meta.json")):
        meta = _read_json(meta_path)
        if meta.get("Name") != context_name:
            continue
        endpoints = meta.get("Endpoints")
        if not isinstance(endpoints, dict):
            return ""
        docker_endpoint = endpoints.get("docker")
        if not isinstance(docker_endpoint, dict):
            return ""
        host = docker_endpoint.get("Host")
        return host.strip() if isinstance(host, str) else ""
    return ""


def _docker_notes(docker_cli: str, docker_socket: str) -> list[str]:
    notes: list[str] = []
    if docker_cli.startswith("available") and docker_socket == "missing":
        notes.append(
            "Docker CLI is installed, but no Docker socket is visible; start the daemon or set DOCKER_HOST for rootless/remote Docker."
        )
    elif docker_socket.startswith("permission_denied"):
        notes.append(
            "Docker socket exists but is not accessible; check user group membership, rootless Docker config, or permissions."
        )
    elif docker_cli == "missing" and docker_socket != "missing":
        notes.append("Docker socket is visible, but the docker CLI is not in PATH.")
    return notes


def _clean_env(key: str) -> str:
    return os.getenv(key, "").strip()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"
