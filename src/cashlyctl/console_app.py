from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, Static

try:
    from pyfiglet import Figlet
except Exception:
    Figlet = None  # type: ignore[assignment]

from cashlyctl.audit import audit_command
from cashlyctl.aws_drilldown import AwsInstanceDetail, aws_sdk_available, fetch_instance_detail
from cashlyctl.auth import load_login_credentials, verify_login
from cashlyctl.commands import CommandKind, parse_command
from cashlyctl.config import (
    QUERIES_DIR,
    STATE_DIR,
    ensure_state_layout,
    load_config,
    save_config,
)
from cashlyctl.deployments import (
    DeployReadinessResult,
    DeployRunResult,
    DeploySpec,
    load_deploy_specs,
    probe_deploy_readiness,
    probe_deploy_target_readiness,
    run_deploy_via_ssh,
    run_rollback_via_ssh,
)
from cashlyctl.health import run_mvp_checks
from cashlyctl.models import DeploymentMode, Environment, HealthCheckResult, Profile
from cashlyctl.network_probe import NetworkProbeResult, NetworkProbeTarget, probe_targets


class AppState(StrEnum):
    PREAUTH = "PREAUTH"
    OBSERVE = "OBSERVE"
    MAINT = "MAINT"
    SERVICE = "SERVICE"


class LogonFlowStage(StrEnum):
    NONE = "NONE"
    USER = "USER"
    PASSWORD = "PASSWORD"


STATE_PALETTES: dict[AppState, dict[str, str]] = {
    AppState.PREAUTH: {
        "body": "#ffffff",
        "accent": "#00a8ff",
        "prompt": "#00a8ff",
    },
    AppState.OBSERVE: {
        "body": "#00ff00",
        "accent": "#00ff00",
        "prompt": "#00ff00",
    },
    AppState.MAINT: {
        "body": "#ffff00",
        "accent": "#ffd700",
        "prompt": "#ffff00",
    },
    AppState.SERVICE: {
        "body": "#ff0000",
        "accent": "#ff3030",
        "prompt": "#ff0000",
    },
}


WRITE_IMPACT_MENU_ITEMS = {"4", "7", "8"}


class CashlyConsoleApp(App[None]):
    TITLE = "cashly ctl"
    CSS = """
    Screen {
        background: black;
        color: white;
    }

    #screen_text {
        height: 1fr;
        padding: 0 1;
        color: white;
    }

    #command_bar {
        height: 1;
        padding: 0 1;
    }

    #command_label {
        width: auto;
        color: yellow;
        text-style: bold;
        padding: 0;
        margin: 0;
    }

    #command_line {
        border: none;
        background: black;
        color: white;
        padding: 0;
        margin: 0;
    }

    #pf_footer {
        height: 1;
        color: cyan;
        padding: 0 1;
    }

    #operator_strip {
        height: 1;
        color: green;
        padding: 0 1;
    }

    #confirm_bar {
        height: 1;
        color: red;
        padding: 0 1;
    }

    #status_line {
        height: 1;
        color: green;
        padding: 0 1;
    }

    #status_line.ok {
        color: green;
    }

    #status_line.warn {
        color: yellow;
    }

    #status_line.error {
        color: red;
    }

    #bottom_rule {
        height: 1;
        color: cyan;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("f1", "help_key", "PF1 Help"),
        Binding("f3", "back", "PF3 Back"),
        Binding("f4", "exit_request", "PF4 Exit"),
        Binding("f5", "refresh", "PF5 Refresh"),
        Binding("f7", "scroll_up", "PF7 Up"),
        Binding("f8", "scroll_down", "PF8 Down"),
        Binding("f9", "pf9", "PF9 Cmd"),
        Binding("f12", "cancel", "PF12 Cancel"),
    ]

    MENU_ITEMS = {
        "1": "Systems Status",
        "2": "Neo4j Console",
        "3": "DealSense Console",
        "4": "Jobs / Runs",
        "5": "Architecture",
        "6": "Security / Drift",
        "7": "Deployments",
        "8": "Utilities",
    }

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.active_profile = self.config.get_active_profile()
        self.os_user = os.getenv("USERNAME") or os.getenv("USER") or "ops"
        self.session_user = "NOT LOGGED ON"
        self.session_role = "guest"
        self.authenticated = False
        self.login_credentials = load_login_credentials()
        self.logo_text = os.getenv("CASHLYCTL_ASCII_TEXT", "cashlyCTL")
        self.logo_font = os.getenv("CASHLYCTL_ASCII_FONT", "slant")
        self.host_ip_label, self.host_ip = _detect_banner_ip()
        self.selected_postauth_state = AppState.OBSERVE
        self.target_state_selected = False
        self.job_status = "IDLE"
        self.logon_stage = LogonFlowStage.NONE
        self.pending_logon_user = ""
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.service_idle_ttl_seconds = _service_idle_ttl_seconds()
        self.service_last_activity_ts = time.monotonic()
        self.jobs_session_id = ""
        self.jobs_session_started_at = ""
        self.jobs_session_file: Path | None = None
        self.jobs_records: list[dict[str, str]] = []
        self.boot_checks_lines: list[str] = []
        self.boot_checks_sequence: list[tuple[str, str, str]] = []
        self.boot_checks_index = 0
        self.boot_checks_running = False
        self.boot_checks_wait_for_enter = False
        self.boot_scroll_offset = 0
        self.boot_checks_timer = None
        self.app_state = AppState.PREAUTH

        self.panel = "L"
        self.back_stack: list[str] = []
        self.exit_armed = False
        self.health_results: list[HealthCheckResult] = run_mvp_checks(self.active_profile)
        self.network_probe_targets: list[NetworkProbeTarget] = _default_network_probe_targets()
        self.network_probe_results: list[NetworkProbeResult] = []
        self.network_probe_interval_seconds = 15
        self.network_probe_timeout_seconds = _network_probe_timeout_seconds()
        self.network_probe_latency_warn_ms = _network_probe_latency_warn_ms()
        self.network_probe_tls_warn_days = 14
        self.network_probe_last_refresh_monotonic = 0.0
        self.network_probe_last_refresh_at = "-"
        self.instance_detail_target = ""
        self.instance_detail_data: AwsInstanceDetail | None = None
        self.instance_detail_last_refresh_at = "-"
        self.instance_detail_cache: dict[str, AwsInstanceDetail] = {}
        self.aws_sdk_ready = aws_sdk_available()
        self.deploy_specs: dict[str, DeploySpec] = load_deploy_specs(
            [target.name for target in self.network_probe_targets]
        )
        self.deploy_readiness: dict[str, DeployReadinessResult] = {}
        self.deploy_readiness_last_refresh_at = "-"
        self.deploy_readiness_timeout_seconds = _deploy_readiness_timeout_seconds()
        self.deploy_readiness_loading = False
        self.deploy_readiness_spinner_frames = ("|", "/", "-", "\\")
        self.deploy_readiness_spinner_index = 0
        self.deploy_readiness_seq = 0
        self.deploy_history: list[dict[str, str]] = []
        self.last_deploy_report: DeployRunResult | None = None

        self.panel1_mode = "STATUS"
        self.tail_target = "neo4j-dev"
        self.tail_lines = 50
        self.log_scroll_offset = 0

        self.panel2_mode = "QUERY"
        self.saved_query: tuple[str, str] | None = None
        self.neo4j_query = "\n".join(
            [
                "MATCH (l:Lender {name:$lender})<-[:FOR_LENDER]-(d:Deal)",
                "WHERE d.created_at >= datetime() - duration('P30D')",
                "RETURN l.name AS lender,",
                "       count(d) AS deals_30d,",
                "       avg(d.ltv) AS avg_ltv,",
                "       avg(d.ds)  AS avg_dealsense_score",
                "ORDER BY deals_30d DESC;",
            ]
        )
        self.neo4j_params = '{ lender: "Hosper" }'

    def compose(self) -> ComposeResult:
        yield Static("", id="screen_text")
        with Horizontal(id="command_bar"):
            yield Static("Command ===>", id="command_label")
            yield Input(placeholder="", id="command_line")
        yield Static("", id="operator_strip")
        yield Static("", id="confirm_bar")
        yield Static("", id="pf_footer")
        yield Static("", id="status_line")
        yield Static("", id="bottom_rule")

    def on_mount(self) -> None:
        self._render()
        self._set_command_mode()
        self.query_one("#command_line", Input).focus()
        self.set_interval(1.0, self._on_clock_tick)
        self.set_interval(0.25, self._on_spinner_tick)
        if self.login_credentials:
            self._set_status("ENTER LOGON TO START AUTH", "ok")
        else:
            self._set_status("NO LOGIN CREDENTIALS FOUND (.env)", "error")

    def on_resize(self, _: events.Resize) -> None:
        self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        self.query_one("#command_line", Input).value = ""
        if self.boot_checks_running or self.boot_checks_wait_for_enter:
            self._handle_boot_checks_input(raw)
            return
        if not raw:
            return
        self._record_activity()
        if self.logon_stage != LogonFlowStage.NONE:
            self._handle_logon_stage_input(raw)
            return
        parsed = parse_command(raw)
        if parsed.kind == CommandKind.LOGON:
            audit_command(
                self.active_profile.name,
                self._redacted_logon_audit(parsed.value),
            )
            self._record_session_job("COMMAND", self._redacted_logon_audit(parsed.value))
        else:
            audit_command(self.active_profile.name, raw)
            self._record_session_job("COMMAND", raw)
        self._execute(raw)

    def action_help_key(self) -> None:
        self._record_activity()
        self._set_job_status("SHOWING HELP...")
        self._set_status(
            "HELP: LOGON, LOGOFF, SET STATE <observe|maint|service>, SERVICE ON, PROCEED <target>, =0..=8, EXIT, REFRESH, PROFILE, SET ENV, TAIL, DETAIL, DEPLOY, ROLLBACK, STATUS DEPLOY, DIFF, PLAN, SAVE QRY",
            "ok",
        )
        self._set_job_status("HELP READY")

    def action_back(self) -> None:
        self._record_activity()
        if self.boot_checks_running or self.boot_checks_wait_for_enter:
            self._set_job_status("BOOT CHECKS ACTIVE")
            self._set_status("SYSTEM CHECKS IN PROGRESS. WAIT OR PRESS ENTER WHEN READY.", "warn")
            return
        self._set_job_status("NAVIGATING BACK...")
        if self.panel == "1" and self.panel1_mode == "LOG_VIEW":
            self.panel1_mode = "STATUS"
            self.log_scroll_offset = 0
            self._render()
            self._set_job_status("BACK COMPLETE")
            self._set_status("BACK TO SYSTEMS STATUS", "ok")
            return
        if self.panel == "2" and self.panel2_mode == "SAVED":
            self.panel2_mode = "QUERY"
            self._render()
            self._set_job_status("BACK COMPLETE")
            self._set_status("BACK TO QUERY VIEW", "ok")
            return
        if self.back_stack:
            self.panel = self.back_stack.pop()
            self._render()
            self._set_job_status("BACK COMPLETE")
            self._set_status("BACK", "ok")
            return
        top_panel = "0" if self.authenticated else "L"
        if self.panel != top_panel:
            self.panel = top_panel
            self._render()
            self._set_job_status("BACK COMPLETE")
            self._set_status("BACK", "ok")
            return
        self._set_job_status("BACK IDLE")
        self._set_status("AT TOP PANEL", "warn")

    def action_exit_request(self) -> None:
        self._record_activity()
        self._set_job_status("EXIT REQUESTED...")
        self._handle_exit()

    def action_refresh(self) -> None:
        self._record_activity()
        self._set_job_status("REFRESHING SYSTEM STATUS...")
        include_status = self.panel in {"1", "I"}
        self._refresh_system_status(
            force_network=include_status,
            include_network=include_status,
        )
        if self.panel == "I" and self.instance_detail_target:
            self._load_instance_detail(self.instance_detail_target, force=True)
        deploy_refresh_started = False
        if self.panel == "7":
            self._start_deploy_readiness_refresh("REFRESH")
            deploy_refresh_started = True
        self.login_credentials = load_login_credentials()
        self.host_ip_label, self.host_ip = _detect_banner_ip()
        self._render()
        if deploy_refresh_started:
            return
        self._set_job_status("REFRESH COMPLETE")
        self._set_status(f"REFRESHED {datetime.now().strftime('%H:%M:%S')}", "ok")

    def action_scroll_up(self) -> None:
        self._record_activity()
        if self.panel == "B":
            max_offset = max(0, len(self._boot_content_lines()) - self._boot_viewport_size())
            if max_offset <= 0:
                self._set_status("NO BOOT CHECK SCROLLBACK AVAILABLE", "warn")
                return
            self.boot_scroll_offset = min(self.boot_scroll_offset + 1, max_offset)
            self._render()
            self._set_status("BOOT CHECKS SCROLLED UP", "ok")
            return
        if self.panel == "1" and self.panel1_mode == "LOG_VIEW":
            self.log_scroll_offset = min(self.log_scroll_offset + 1, 1000)
            self._render()
            self._set_status("LOG VIEW SCROLLED UP", "ok")
            return
        self._set_status("PF7 AVAILABLE IN LOG VIEW", "warn")

    def action_scroll_down(self) -> None:
        self._record_activity()
        if self.panel == "B":
            if self.boot_scroll_offset == 0:
                self._set_status("ALREADY AT LATEST BOOT CHECK OUTPUT", "warn")
                return
            self.boot_scroll_offset = max(self.boot_scroll_offset - 1, 0)
            self._render()
            self._set_status("BOOT CHECKS SCROLLED DOWN", "ok")
            return
        if self.panel == "1" and self.panel1_mode == "LOG_VIEW":
            self.log_scroll_offset = max(self.log_scroll_offset - 1, 0)
            self._render()
            self._set_status("LOG VIEW SCROLLED DOWN", "ok")
            return
        self._set_status("PF8 AVAILABLE IN LOG VIEW", "warn")

    def action_pf9(self) -> None:
        self._record_activity()
        self._set_job_status("PF9 ACTION...")
        if self.panel == "2":
            self.panel2_mode = "QUERY"
            self.saved_query = None
            self._render()
            self._set_job_status("QUERY RUN COMPLETE")
            self._set_status("QUERY RUN (MOCK): rows=1 time=84ms", "ok")
            return
        self.query_one("#command_line", Input).focus()
        self._set_job_status("CMD READY")
        self._set_status("CMD READY", "ok")

    def action_cancel(self) -> None:
        self._record_activity()
        if self.boot_checks_running or self.boot_checks_wait_for_enter:
            self._set_job_status("BOOT CHECKS ACTIVE")
            self._set_status("SYSTEM CHECKS IN PROGRESS. WAIT OR PRESS ENTER WHEN READY.", "warn")
            return
        self._set_job_status("CANCEL REQUEST...")
        if self.logon_stage != LogonFlowStage.NONE:
            self._end_logon_flow()
            self._set_job_status("AUTH CANCELED")
            self._set_status("LOGON CANCELED", "warn")
            return
        if self.panel == "1" and self.panel1_mode == "LOG_VIEW":
            self.panel1_mode = "STATUS"
            self.log_scroll_offset = 0
            self._render()
            self._set_job_status("CANCEL COMPLETE")
            self._set_status("LOG VIEW CANCELED", "ok")
            return
        if self.panel == "2" and self.panel2_mode == "SAVED":
            self.panel2_mode = "QUERY"
            self.saved_query = None
            self._render()
            self._set_job_status("CANCEL COMPLETE")
            self._set_status("SAVE NOTICE CANCELED", "ok")
            return
        self._set_job_status("CANCEL IDLE")
        self._set_status("NOTHING TO CANCEL", "warn")

    def _execute(self, raw: str) -> None:
        parsed = parse_command(raw)
        if parsed.kind != CommandKind.EXIT:
            self.exit_armed = False

        if parsed.kind == CommandKind.EMPTY:
            return
        if parsed.kind == CommandKind.HELP:
            self.action_help_key()
            return
        if parsed.kind == CommandKind.EXIT:
            self._handle_exit()
            return
        if parsed.kind == CommandKind.LOGON:
            self._handle_logon(parsed.value)
            return
        if parsed.kind == CommandKind.LOGOFF:
            self._handle_logoff()
            return
        if parsed.kind == CommandKind.SERVICE_ON:
            self._start_service_confirmation()
            return
        if parsed.kind == CommandKind.PROCEED:
            self._confirm_service_enable(parsed.value)
            return
        if parsed.kind == CommandKind.REFRESH:
            self.action_refresh()
            return
        if parsed.kind == CommandKind.PROFILE:
            self._goto_panel("P")
            self._set_status("PROFILE PICKER", "ok")
            return
        if parsed.kind == CommandKind.SET_ENV:
            self._set_env(parsed.value)
            return
        if parsed.kind == CommandKind.SET_STATE:
            self._set_requested_state(parsed.value)
            return
        if parsed.kind == CommandKind.NUMBER and self.panel in {"L", "P"}:
            self._handle_number(parsed.value)
            return

        if not self.authenticated:
            self._set_status("LOGON REQUIRED: TYPE LOGON", "warn")
            return

        if parsed.kind == CommandKind.MENU_ROOT:
            self._goto_panel("0")
            self._set_status("PRIMARY MENU", "ok")
            return
        if parsed.kind == CommandKind.PANEL_JUMP:
            self._jump_to_panel(parsed.value)
            return
        if parsed.kind == CommandKind.TAIL:
            self._handle_tail(parsed.value)
            return
        if parsed.kind == CommandKind.DETAIL:
            self._handle_detail(parsed.value)
            return
        if parsed.kind == CommandKind.PLAN:
            self._handle_plan(parsed.value)
            return
        if parsed.kind == CommandKind.STATUS_DEPLOY:
            self._handle_status_deploy(parsed.value)
            return
        if parsed.kind == CommandKind.DIFF:
            self._handle_diff(parsed.value)
            return
        if parsed.kind == CommandKind.DEPLOY:
            self._handle_deploy(parsed.value)
            return
        if parsed.kind == CommandKind.ROLLBACK:
            self._handle_rollback(parsed.value)
            return
        if parsed.kind == CommandKind.SAVE_QRY:
            self._save_query(parsed.value)
            return
        if parsed.kind == CommandKind.NUMBER:
            self._handle_number(parsed.value)
            return

        upper = raw.upper()
        if self.panel == "2" and upper == "RUN":
            self.panel2_mode = "QUERY"
            self.saved_query = None
            self._render()
            self._set_job_status("QUERY RUN COMPLETE")
            self._set_status("QUERY RUN (MOCK): rows=1 time=84ms", "ok")
            return
        self._set_job_status("COMMAND FAILED")
        self._set_status(f"UNKNOWN COMMAND: {raw}", "error")

    def _handle_logon(self, logon_args: str) -> None:
        args = logon_args.strip()
        if self.authenticated:
            self._set_status(f"ALREADY LOGGED ON AS {self.session_user}", "warn")
            self._set_job_status("AUTH IDLE")
            return

        if not args:
            self._start_logon_user_prompt()
            return

        parts = args.split()
        self._start_logon_password_prompt(parts[0])
        if len(parts) > 1:
            self._set_status(
                "INLINE PASSWORD IGNORED. ENTER PASSWORD IN MASKED PROMPT.",
                "warn",
            )

    def _handle_logon_stage_input(self, raw: str) -> None:
        stage_command = parse_command(raw)
        if stage_command.kind == CommandKind.LOGOFF:
            self._handle_logoff()
            return

        if self.logon_stage == LogonFlowStage.USER:
            username = raw.strip().split()[0] if raw.strip() else ""
            if not username:
                self._set_status("USERID CANNOT BE EMPTY", "error")
                self._set_job_status("AUTH FAILED")
                return
            self._start_logon_password_prompt(username)
            return

        if self.logon_stage == LogonFlowStage.PASSWORD:
            username = self.pending_logon_user
            if not username:
                self._end_logon_flow()
                self._set_status("LOGON FLOW RESET. TYPE LOGON AGAIN.", "warn")
                self._set_job_status("AUTH IDLE")
                return
            password = raw
            audit_command(self.active_profile.name, f"LOGON {username} ****")
            self._end_logon_flow()
            self._authenticate_logon(username, password)
            return

        self._end_logon_flow()
        audit_command(self.active_profile.name, raw)
        self._execute(raw)

    def _start_logon_user_prompt(self) -> None:
        self.logon_stage = LogonFlowStage.USER
        self.pending_logon_user = ""
        self._set_command_mode(label="User ===>", password=False, placeholder="userid")
        self._set_job_status("AUTH: USERID REQUIRED")
        self._set_status("ENTER USERID", "ok")

    def _start_logon_password_prompt(self, username: str) -> None:
        user = username.strip()
        if not user:
            self._set_status("USERID CANNOT BE EMPTY", "error")
            self._set_job_status("AUTH FAILED")
            return
        self.logon_stage = LogonFlowStage.PASSWORD
        self.pending_logon_user = user
        self._set_command_mode(label="Pass ===>", password=True, placeholder="****")
        self._set_job_status("AUTH: PASSWORD REQUIRED")
        self._set_status(f"ENTER PASSWORD FOR {user}", "ok")

    def _end_logon_flow(self) -> None:
        self.logon_stage = LogonFlowStage.NONE
        self.pending_logon_user = ""
        self._set_command_mode()

    def _authenticate_logon(self, username: str, password: str) -> None:
        self._set_job_status("AUTHENTICATING USER...")
        user = username.strip()
        if not user or not password:
            self._set_status("USAGE: LOGON then USERID then PASSWORD", "error")
            self._set_job_status("AUTH FAILED")
            return

        self.login_credentials = load_login_credentials()
        if not self.login_credentials:
            self._set_status("NO LOGIN CREDENTIALS FOUND (.env)", "error")
            self._set_job_status("AUTH FAILED")
            return
        if not verify_login(user, password, self.login_credentials):
            self._set_status("LOGON FAILED: INVALID CREDENTIALS", "error")
            self._set_job_status("AUTH FAILED")
            self.panel = "L"
            self._render()
            return

        if not self.target_state_selected:
            self.selected_postauth_state = AppState.OBSERVE

        requested_state = self.selected_postauth_state
        role = self._role_for_user(user)
        if not self._role_allows_state(role, requested_state):
            self._set_status(
                f"ACCESS DENIED: {role.upper()} CANNOT LOGON TO {requested_state.value}",
                "error",
            )
            self._set_job_status("AUTH FAILED")
            self.panel = "L"
            self._render()
            return

        self.authenticated = True
        self.session_user = user
        self.session_role = role
        self.panel = "B"
        self.back_stack.clear()
        self.service_last_activity_ts = time.monotonic()
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self._start_jobs_session(user, role, requested_state)
        self._set_job_status(f"AUTH COMPLETE ({role.upper()})", render_now=False)
        self._start_boot_checks()
        self._set_status(
            f"LOGON ACCEPTED: {user} ({role}) -> RUNNING SYSTEM CHECKS",
            "ok",
        )

    def _handle_exit(self) -> None:
        if self.active_profile.env == Environment.PROD and not self.exit_armed:
            self.exit_armed = True
            self._set_job_status("EXIT CONFIRMATION REQUIRED")
            self._set_status("PROD SAFETY: TYPE EXIT AGAIN TO CONFIRM", "warn")
            return
        self._set_job_status("EXITING APPLICATION")
        self.exit()

    def _handle_logoff(self) -> None:
        self._set_job_status("LOGGING OFF...", render_now=False)
        had_session = self.authenticated
        self._record_session_job("SESSION_END", f"{self.session_user} logged off", "OK")
        self._stop_boot_checks()

        if self.logon_stage != LogonFlowStage.NONE:
            self._end_logon_flow()

        self.authenticated = False
        self.session_user = "NOT LOGGED ON"
        self.session_role = "guest"
        self.panel = "L"
        self.back_stack.clear()
        self.exit_armed = False

        self.panel1_mode = "STATUS"
        self.panel2_mode = "QUERY"
        self.saved_query = None
        self.log_scroll_offset = 0

        self.selected_postauth_state = AppState.OBSERVE
        self.target_state_selected = False
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.service_last_activity_ts = time.monotonic()
        self.jobs_session_id = ""
        self.jobs_session_started_at = ""
        self.jobs_session_file = None
        self.jobs_records = []
        self.boot_checks_lines = []
        self.boot_checks_sequence = []
        self.boot_checks_index = 0
        self.boot_checks_wait_for_enter = False
        self.boot_scroll_offset = 0
        self.app_state = AppState.PREAUTH
        self.instance_detail_target = ""
        self.instance_detail_data = None
        self.instance_detail_last_refresh_at = "-"
        self.instance_detail_cache = {}
        self.deploy_history = []
        self.last_deploy_report = None
        self.deploy_readiness = {}
        self.deploy_readiness_last_refresh_at = "-"
        self.deploy_readiness_loading = False

        self._set_job_status("SESSION PREAUTH", render_now=False)
        self._render()
        self.refresh()
        if had_session:
            self._set_status("LOGOFF COMPLETE", "ok")
        else:
            self._set_status("ALREADY IN PREAUTH", "warn")

    def _jump_to_panel(self, panel_code: str) -> None:
        self._set_job_status(f"OPENING PANEL {panel_code}...")
        if panel_code == "0":
            self._goto_panel("0")
            self._set_job_status("PANEL 0 READY")
            self._set_status("PRIMARY MENU", "ok")
            return
        if panel_code in self.MENU_ITEMS:
            if panel_code == "5" and self.active_profile.mode == DeploymentMode.ENTERPRISE:
                self._set_job_status("PANEL OPEN BLOCKED")
                self._set_status("PANEL 5 DISABLED IN ENTERPRISE MODE", "warn")
                return
            self._goto_panel(panel_code)
            if panel_code == "1":
                self._refresh_system_status(
                    force_network=not self.network_probe_results,
                    include_network=True,
                )
                self._render()
            if panel_code == "7":
                self._start_deploy_readiness_refresh("PANEL OPEN")
                self._set_job_status("PANEL 7 LOADING PRECHECKS...", render_now=False)
                self._set_status("PANEL 7 OPEN. DEPLOY PRECHECKS RUNNING...", "warn")
                return
            self._set_job_status(f"PANEL {panel_code} READY")
            self._set_status(f"PANEL {panel_code} OPEN", "ok")
            return
        self._set_job_status("PANEL OPEN FAILED")
        self._set_status(f"INVALID PANEL: ={panel_code}", "error")

    def _handle_number(self, value: str) -> None:
        if self.panel == "L" and not self.authenticated:
            if value == "1":
                self._set_requested_state("observe")
                return
            if value == "2":
                self._set_requested_state("maint")
                return
            if value == "3":
                self._set_requested_state("service")
                return
            self._set_status("INVALID PREAUTH OPTION. USE 1, 2, OR 3.", "warn")
            return
        if self.panel == "0":
            self._jump_to_panel(value)
            return
        if self.panel == "P":
            self._pick_profile_by_index(value)
            return
        if self.panel == "1" and self.panel1_mode == "STATUS":
            self._open_instance_detail_by_index(value)
            return
        if self.panel == "7":
            self._handle_deployments_menu_number(value)
            return
        self._set_status(f"NO NUMERIC ACTION ON PANEL {self.panel}", "warn")

    def _handle_tail(self, tail_args: str) -> None:
        self._set_job_status("OPENING LOG VIEW...")
        if self.panel != "1":
            self._set_status("TAIL IS ONLY AVAILABLE ON PANEL 1", "warn")
            self._set_job_status("LOG VIEW IDLE")
            return
        match = re.match(r"(?i)^([a-z0-9_-]+)(?:\s+(\d+))?$", tail_args.strip())
        if not match:
            self._set_status("USAGE: TAIL <service> <n>", "error")
            self._set_job_status("LOG VIEW FAILED")
            return
        target = match.group(1).lower()
        line_count = int(match.group(2) or "50")
        if line_count <= 0:
            self._set_status("TAIL COUNT MUST BE > 0", "error")
            self._set_job_status("LOG VIEW FAILED")
            return
        self.tail_target = _tail_target_label(target)
        self.tail_lines = min(line_count, 500)
        self.log_scroll_offset = 0
        self.panel1_mode = "LOG_VIEW"
        self._render()
        self._set_job_status("LOG VIEW ACTIVE")
        self._set_status(f"TAIL ACTIVE: {self.tail_target} ({self.tail_lines})", "ok")

    def _handle_detail(self, detail_arg: str) -> None:
        if self.panel not in {"1", "I"}:
            self._set_status("DETAIL IS AVAILABLE ON PANEL 1 OR INSTANCE DETAIL PANEL", "warn")
            return
        raw = detail_arg.strip()
        if not raw:
            if self.panel == "I" and self.instance_detail_target:
                self._load_instance_detail(self.instance_detail_target, force=True)
                self._render()
                self._set_status(f"INSTANCE DETAIL REFRESHED: {self.instance_detail_target}", "ok")
                return
            self._set_status("USAGE: DETAIL <row_number|instance_name>", "warn")
            return
        if raw.isdigit():
            self._open_instance_detail_by_index(raw)
            return
        self._open_instance_detail(raw)

    def _open_instance_detail_by_index(self, index_text: str) -> None:
        if not self.network_probe_results:
            self._set_status("NO NETWORK TARGETS AVAILABLE. REFRESH PANEL 1 FIRST.", "warn")
            return
        try:
            idx = int(index_text)
        except ValueError:
            self._set_status(f"INVALID ROW: {index_text}", "error")
            return
        pos = idx - 1
        if pos < 0 or pos >= len(self.network_probe_results):
            self._set_status(f"ROW OUT OF RANGE: {idx}", "error")
            return
        self._open_instance_detail(self.network_probe_results[pos].name)

    def _open_instance_detail(self, target_name: str) -> None:
        if not self.network_probe_results:
            self._set_status("NO NETWORK TARGETS AVAILABLE. REFRESH PANEL 1 FIRST.", "warn")
            return
        lookup = target_name.strip().lower()
        matched = next(
            (item.name for item in self.network_probe_results if item.name.lower() == lookup),
            "",
        )
        if not matched:
            matched = next(
                (item.name for item in self.network_probe_results if lookup in item.name.lower()),
                "",
            )
        if not matched:
            self._set_status(f"INSTANCE NOT FOUND: {target_name}", "error")
            return

        self._set_job_status(f"LOADING INSTANCE DETAIL: {matched}...", render_now=False)
        self.instance_detail_target = matched
        self._load_instance_detail(matched, force=True)
        self._goto_panel("I")
        if self.instance_detail_data and self.instance_detail_data.error:
            self._set_status(
                f"INSTANCE DETAIL PARTIAL: {self.instance_detail_data.error}",
                "warn",
            )
            self._set_job_status(f"INSTANCE DETAIL READY (PARTIAL): {matched}", render_now=False)
        else:
            self._set_status(f"INSTANCE DETAIL READY: {matched}", "ok")
            self._set_job_status(f"INSTANCE DETAIL READY: {matched}", render_now=False)
        self._render()

    def _load_instance_detail(self, target_name: str, force: bool = False) -> None:
        normalized = target_name.strip().lower()
        if not normalized:
            return
        if not force and normalized in self.instance_detail_cache:
            self.instance_detail_data = self.instance_detail_cache[normalized]
            self.instance_detail_last_refresh_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return
        target_url = self._target_url_for_name(target_name)
        detail = fetch_instance_detail(target_name, target_url)
        self.instance_detail_cache[normalized] = detail
        self.instance_detail_data = detail
        self.instance_detail_last_refresh_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _target_url_for_name(self, target_name: str) -> str:
        lookup = target_name.strip().lower()
        for target in self.network_probe_targets:
            if target.name.lower() == lookup:
                return target.url
        return ""

    def _handle_deployments_menu_number(self, value: str) -> None:
        mapping = {
            "1": "crm-dev",
            "2": "crm-prod",
            "3": "n8n-server",
        }
        if value in mapping:
            target = mapping[value]
            if self.selected_postauth_state == AppState.OBSERVE:
                self._handle_plan(f"show {target}")
            else:
                self._handle_deploy(target)
            return
        if value == "4":
            self._set_status("ROLLBACK USAGE: ROLLBACK <target> --to <ref|last-good>", "warn")
            return
        if value == "5":
            self._set_status("DEPLOY HISTORY DISPLAYED BELOW", "ok")
            self._set_job_status("DEPLOY HISTORY READY", render_now=False)
            self._render()
            return
        if value == "6":
            self._set_status("RELEASE MANAGEMENT: COMING LATER", "warn")
            return
        self._set_status("INVALID DEPLOYMENTS OPTION. USE 1..6", "warn")

    def _handle_plan(self, plan_args: str) -> None:
        raw = plan_args.strip()
        if raw.lower().startswith("show "):
            target = raw[5:].strip().lower()
        else:
            target = raw.lower()
        if not target:
            self._set_status("USAGE: PLAN show <target>", "warn")
            return
        target = self._normalize_deploy_target(target)
        if not target:
            self._set_status("UNKNOWN TARGET FOR PLAN", "error")
            return

        spec = self._deploy_spec_for_target(target)
        readiness = probe_deploy_target_readiness(
            target,
            spec,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )
        self.deploy_readiness[target.lower()] = readiness
        self.deploy_readiness_last_refresh_at = readiness.checked_at
        self._render()

        fail_checks = [step.name for step in readiness.checks if step.status == "FAIL"]
        warn_checks = [step.name for step in readiness.checks if step.status == "WARN"]
        mode_note = "SERVICE REQUIRED FOR PROD" if self._target_requires_service(target) else "MAINT OR SERVICE"
        if readiness.status == "FAIL":
            self._set_job_status("PLAN BLOCKED", render_now=False)
            checks_text = ", ".join(fail_checks[:3]) if fail_checks else "preflight"
            self._set_status(
                f"PLAN {target}: NOT READY ({checks_text}) - refresh panel for details",
                "error",
            )
            return

        if readiness.status == "WARN":
            self._set_job_status(f"PLAN WARN: {target.upper()}", render_now=False)
            warn_text = ", ".join(warn_checks[:3]) if warn_checks else "non-blocking"
            self._set_status(
                f"PLAN {target}: READY WITH WARNINGS ({warn_text}) ({mode_note})",
                "warn",
            )
            return

        self._set_job_status(f"PLAN READY: {target.upper()}", render_now=False)
        self._set_status(
            f"PLAN {target}: READY preflight -> fetch/build -> pm2/nginx -> verify -> record ({mode_note})",
            "ok",
        )

    def _handle_status_deploy(self, status_args: str) -> None:
        target = self._normalize_deploy_target(status_args.strip().lower())
        if not target:
            self._set_status("USAGE: STATUS DEPLOY <target>", "warn")
            return
        latest = self._latest_deploy_record(target)
        if not latest:
            self._set_status(f"NO DEPLOY HISTORY FOR {target}", "warn")
            return
        self._set_status(
            f"DEPLOY STATUS {target}: {latest.get('status', '-')} @ {latest.get('finished_at', '-')}",
            "ok",
        )

    def _handle_diff(self, diff_args: str) -> None:
        raw = diff_args.strip()
        if not raw:
            self._set_status("USAGE: DIFF <target> --current --target <sha>", "warn")
            return
        target = self._normalize_deploy_target(raw.split()[0].lower())
        if not target:
            self._set_status("UNKNOWN TARGET FOR DIFF", "error")
            return
        self._set_job_status(f"DIFF READY: {target.upper()}", render_now=False)
        self._set_status(
            f"DIFF {target}: command accepted (implementation stub for git/current vs target)",
            "ok",
        )

    def _handle_deploy(self, deploy_args: str) -> None:
        target, revision, tag, error = self._parse_deploy_args(deploy_args)
        if error:
            self._set_status(error, "error")
            return
        if not target:
            self._set_status("USAGE: DEPLOY <target> [REV <sha>|TAG <tag>]", "warn")
            return

        target = self._normalize_deploy_target(target)
        if not target:
            self._set_status("UNKNOWN DEPLOY TARGET", "error")
            return
        if not self._mode_allows_write(target):
            return

        spec = self._deploy_spec_for_target(target)
        if not spec:
            self._set_status(f"NO DEPLOY SSH CONFIG FOR {target}", "error")
            self._set_job_status("DEPLOY FAILED")
            return

        readiness = probe_deploy_target_readiness(
            target,
            spec,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )
        self.deploy_readiness[target.lower()] = readiness
        self.deploy_readiness_last_refresh_at = readiness.checked_at
        if readiness.status == "FAIL":
            fail_step = next((step for step in readiness.checks if step.status == "FAIL"), None)
            reason = fail_step.name if fail_step else "PRECHECK"
            self._set_job_status("DEPLOY BLOCKED")
            self._set_status(f"DEPLOY BLOCKED: {target} not ready ({reason})", "error")
            self._render()
            return

        if readiness.status == "WARN":
            self._set_status(
                f"DEPLOY PRECHECK WARNINGS: {target} (continuing)",
                "warn",
            )

        self._set_job_status(f"DEPLOY RUNNING: {target.upper()}...", render_now=False)
        self._render()
        report = run_deploy_via_ssh(spec, revision=revision, tag=tag)
        self.last_deploy_report = report
        self._record_deploy_report(report)
        self._set_job_status(f"DEPLOY {report.status}: {target.upper()}", render_now=False)
        level = "ok" if report.status == "OK" else ("warn" if report.status == "WARN" else "error")
        self._set_status(
            f"DEPLOY {target} {report.status} ref={report.ref} finished={report.finished_at}",
            level,
        )
        self._render()

    def _handle_rollback(self, rollback_args: str) -> None:
        target, to_ref, error = self._parse_rollback_args(rollback_args)
        if error:
            self._set_status(error, "error")
            return
        if not target:
            self._set_status("USAGE: ROLLBACK <target> --to <ref|last-good>", "warn")
            return
        target = self._normalize_deploy_target(target)
        if not target:
            self._set_status("UNKNOWN ROLLBACK TARGET", "error")
            return
        if not self._mode_allows_write(target):
            return

        spec = self._deploy_spec_for_target(target)
        if not spec:
            self._set_status(f"NO DEPLOY SSH CONFIG FOR {target}", "error")
            self._set_job_status("ROLLBACK FAILED")
            return

        readiness = probe_deploy_target_readiness(
            target,
            spec,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )
        self.deploy_readiness[target.lower()] = readiness
        self.deploy_readiness_last_refresh_at = readiness.checked_at
        if readiness.status == "FAIL":
            fail_step = next((step for step in readiness.checks if step.status == "FAIL"), None)
            reason = fail_step.name if fail_step else "PRECHECK"
            self._set_job_status("ROLLBACK BLOCKED")
            self._set_status(f"ROLLBACK BLOCKED: {target} not ready ({reason})", "error")
            self._render()
            return

        self._set_job_status(f"ROLLBACK RUNNING: {target.upper()}...", render_now=False)
        self._render()
        report = run_rollback_via_ssh(spec, to_ref or "last-good")
        self.last_deploy_report = report
        self._record_deploy_report(report)
        self._set_job_status(f"ROLLBACK {report.status}: {target.upper()}", render_now=False)
        level = "ok" if report.status == "OK" else ("warn" if report.status == "WARN" else "error")
        self._set_status(
            f"ROLLBACK {target} {report.status} ref={report.ref} finished={report.finished_at}",
            level,
        )
        self._render()

    def _record_deploy_report(self, report: DeployRunResult) -> None:
        item = {
            "action": report.action,
            "target": report.target,
            "status": report.status,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "ref": report.ref,
        }
        self.deploy_history.append(item)
        self._record_session_job(
            f"{report.action}_{report.target}",
            f"status={report.status} ref={report.ref} finished={report.finished_at}",
            report.status,
        )
        for step in report.steps:
            self._record_session_job(
                f"{report.action}_STEP_{step.name}",
                step.detail[:400],
                step.status,
            )

    def _latest_deploy_record(self, target: str) -> dict[str, str] | None:
        for item in reversed(self.deploy_history):
            if item.get("target", "").lower() == target.lower():
                return item
        return None

    def _mode_allows_write(self, target: str) -> bool:
        if self.selected_postauth_state == AppState.OBSERVE:
            self._set_status("OBSERVE MODE IS VIEW-ONLY. SWITCH TO MAINT/SERVICE.", "error")
            self._set_job_status("DEPLOY DENIED")
            return False
        if self._target_requires_service(target) and self.selected_postauth_state != AppState.SERVICE:
            self._set_status(f"{target} REQUIRES SERVICE MODE", "error")
            self._set_job_status("DEPLOY DENIED")
            return False
        return True

    def _target_requires_service(self, target: str) -> bool:
        return "prod" in target.lower()

    def _deploy_spec_for_target(self, target: str) -> DeploySpec | None:
        return self.deploy_specs.get(target.lower())

    def _normalize_deploy_target(self, raw: str) -> str:
        value = raw.strip().lower()
        aliases = {
            "crm-dev": "cashly-app-development",
            "crm-prod": "cashly-app-production",
            "crm-development": "cashly-app-development",
            "crm-production": "cashly-app-production",
            "n8n": "n8n-server",
        }
        candidate = aliases.get(value, value)
        for target in self.network_probe_targets:
            if target.name.lower() == candidate:
                return target.name
        return ""

    @staticmethod
    def _parse_deploy_args(raw: str) -> tuple[str, str, str, str]:
        tokens = raw.strip().split()
        if not tokens:
            return "", "", "", ""
        target = tokens[0]
        if len(tokens) == 1:
            return target, "", "", ""
        if len(tokens) >= 3 and tokens[1].upper() == "REV":
            return target, tokens[2], "", ""
        if len(tokens) >= 3 and tokens[1].upper() == "TAG":
            return target, "", tokens[2], ""
        return "", "", "", "USAGE: DEPLOY <target> [REV <sha>|TAG <tag>]"

    @staticmethod
    def _parse_rollback_args(raw: str) -> tuple[str, str, str]:
        tokens = raw.strip().split()
        if not tokens:
            return "", "", ""
        target = tokens[0]
        if len(tokens) == 1:
            return target, "last-good", ""
        if len(tokens) >= 3 and tokens[1].lower() in {"--to", "to"}:
            return target, tokens[2], ""
        return "", "", "USAGE: ROLLBACK <target> --to <ref|last-good>"

    def _save_query(self, query_name: str) -> None:
        self._set_job_status("SAVING QUERY...")
        if self.panel != "2":
            self._set_status("SAVE QRY IS ONLY AVAILABLE ON PANEL 2", "warn")
            self._set_job_status("QUERY SAVE IDLE")
            return
        normalized = _normalize_query_name(query_name)
        if not normalized:
            self._set_status("INVALID QUERY NAME", "error")
            self._set_job_status("QUERY SAVE FAILED")
            return
        profile_dir = _query_profile_dir(self.active_profile)
        ensure_state_layout()
        target_dir = QUERIES_DIR / profile_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{normalized}.cypher"
        file_path.write_text(self.neo4j_query.strip() + "\n", encoding="utf-8")
        display_path = f"~/.cashlyctl/queries/{profile_dir}/{normalized}.cypher"

        self.saved_query = (normalized.upper(), display_path)
        self.panel2_mode = "SAVED"
        self._render()
        self._set_job_status(f"QUERY SAVED: {normalized.upper()}")
        self._set_status(f"SAVED QRY {normalized.upper()}", "ok")

    def _set_requested_state(self, value: str) -> None:
        state = _state_from_text(value)
        if not state:
            self._set_job_status("STATE CHANGE FAILED")
            self._set_status("USAGE: SET STATE <observe|maint|service>", "error")
            return

        if self.authenticated:
            if not self._role_allows_state(self.session_role, state):
                self._set_job_status("STATE CHANGE DENIED")
                self._set_status(
                    f"ACCESS DENIED: {self.session_role.upper()} CANNOT SWITCH TO {state.value}",
                    "error",
                )
                return
            self.service_confirm_required = False
            self.service_confirm_scope = ""
            self.selected_postauth_state = state
            self.app_state = state
            if state == AppState.SERVICE:
                self.service_last_activity_ts = time.monotonic()
            self._set_job_status(f"MODE READY: {state.value}", render_now=False)
            self._set_status(f"MODE CHANGED -> {state.value}", "ok")
            self._render()
            self.refresh()
            return

        self._set_job_status("CHANGING TARGET STATE...", render_now=False)
        self.selected_postauth_state = state
        self.target_state_selected = True
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.app_state = state
        self._render()
        self.refresh()
        self._set_job_status(f"TARGET STATE READY: {state.value}", render_now=False)
        self._set_status(f"TARGET POSTAUTH STATE -> {state.value}", "ok")
        self._render()
        self.refresh()

    def _start_service_confirmation(self) -> None:
        if not self.authenticated:
            self._set_status("LOGON REQUIRED BEFORE SERVICE ARM", "warn")
            self._set_job_status("SERVICE ARM BLOCKED")
            return
        if self.selected_postauth_state == AppState.SERVICE and not self.service_confirm_required:
            self._set_status("SERVICE MODE ALREADY ACTIVE", "warn")
            self._set_job_status("SERVICE MODE ACTIVE")
            return
        if self.session_role != "superadmin":
            self._set_status("SERVICE MODE REQUIRES SUPERADMIN ROLE", "error")
            self._set_job_status("SERVICE ARM DENIED")
            return
        self.service_confirm_scope = self._service_scope_token()
        self.service_confirm_required = True
        self._set_job_status("SERVICE ARM PENDING")
        self._set_status(
            f"CONFIRM SERVICE: TYPE PROCEED {self.service_confirm_scope}",
            "warn",
        )
        self._render()
        self.refresh()

    def _confirm_service_enable(self, scope_value: str) -> None:
        if not self.authenticated:
            self._set_status("LOGON REQUIRED BEFORE PROCEED", "warn")
            self._set_job_status("SERVICE ARM BLOCKED")
            return
        if not self.service_confirm_required:
            self._set_status("NO PENDING SERVICE ARM REQUEST", "warn")
            return
        provided = scope_value.strip().upper()
        expected = self._service_scope_token()
        if provided != expected:
            self._set_status(f"CONFIRMATION MISMATCH. TYPE PROCEED {expected}", "error")
            self._set_job_status("SERVICE ARM FAILED")
            return
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.selected_postauth_state = AppState.SERVICE
        self.app_state = AppState.SERVICE
        self.service_last_activity_ts = time.monotonic()
        self._set_job_status("SERVICE MODE ACTIVE", render_now=False)
        self._set_status("SERVICE MODE ENABLED (WRITE WINDOW OPEN)", "warn")
        self._render()
        self.refresh()

    def _auto_revert_service_mode(self, reason: str) -> None:
        if self.selected_postauth_state != AppState.SERVICE:
            return
        self.selected_postauth_state = AppState.OBSERVE
        self.app_state = AppState.OBSERVE
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self._set_job_status("SERVICE WINDOW CLOSED", render_now=False)
        self._set_status(reason, "warn")
        self._render()
        self.refresh()

    @staticmethod
    def _role_for_user(username: str) -> str:
        user = username.strip().lower()
        env_key = f"CASHLYCTL_ROLE_{user.upper()}"
        env_role = os.getenv(env_key, "").strip().lower()
        if env_role in {"admin", "superadmin"}:
            return env_role
        if user == "superadmin":
            return "superadmin"
        return "admin"

    @staticmethod
    def _role_allows_state(role: str, state: AppState) -> bool:
        if role == "superadmin":
            return state in {AppState.OBSERVE, AppState.MAINT, AppState.SERVICE}
        if role == "admin":
            return state in {AppState.OBSERVE, AppState.MAINT}
        return False

    def _set_env(self, profile_name: str) -> None:
        self._set_job_status("SWITCHING PROFILE...")
        target = self.config.get_profile(profile_name)
        if not target:
            self._set_status(f"PROFILE NOT FOUND: {profile_name}", "error")
            self._set_job_status("PROFILE SWITCH FAILED")
            return
        self._set_active_profile(target)
        self.panel = "0" if self.authenticated else "L"
        self.back_stack.clear()
        self._render()
        self._set_job_status(f"PROFILE ACTIVE: {target.name}")
        self._set_status(f"ACTIVE PROFILE -> {target.name}", "ok")

    def _pick_profile_by_index(self, index_text: str) -> None:
        self._set_job_status("SWITCHING PROFILE...")
        try:
            idx = int(index_text)
        except ValueError:
            self._set_status(f"INVALID PROFILE INDEX: {index_text}", "error")
            self._set_job_status("PROFILE SWITCH FAILED")
            return
        pos = idx - 1
        if pos < 0 or pos >= len(self.config.profiles):
            self._set_status(f"PROFILE INDEX OUT OF RANGE: {idx}", "error")
            self._set_job_status("PROFILE SWITCH FAILED")
            return
        self._set_active_profile(self.config.profiles[pos])
        self.panel = "0" if self.authenticated else "L"
        self.back_stack.clear()
        self._render()
        self._set_job_status(f"PROFILE ACTIVE: {self.active_profile.name}")
        self._set_status(f"PROFILE SELECTED: {self.active_profile.name}", "ok")

    def _set_active_profile(self, profile: Profile) -> None:
        self.active_profile = profile
        self.config.active_profile = profile.name
        save_config(self.config)
        self.network_probe_results = []
        self.network_probe_last_refresh_monotonic = 0.0
        self.network_probe_last_refresh_at = "-"
        self._refresh_system_status(include_network=False)
        self.panel1_mode = "STATUS"
        self.panel2_mode = "QUERY"
        self.saved_query = None
        self.log_scroll_offset = 0
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.instance_detail_target = ""
        self.instance_detail_data = None
        self.instance_detail_last_refresh_at = "-"
        self.instance_detail_cache = {}
        self.deploy_specs = load_deploy_specs([target.name for target in self.network_probe_targets])
        self.deploy_history = []
        self.last_deploy_report = None
        self.deploy_readiness = {}
        self.deploy_readiness_last_refresh_at = "-"
        self.deploy_readiness_loading = False
        self._render()

    def _goto_panel(self, panel: str) -> None:
        if panel == self.panel:
            self._render()
            return
        self.back_stack.append(self.panel)
        self.panel = panel
        if panel != "1":
            self.panel1_mode = "STATUS"
        self._render()

    def _refresh_system_status(
        self,
        force_network: bool = False,
        include_network: bool = True,
    ) -> None:
        self.health_results = run_mvp_checks(self.active_profile)
        if include_network and (
            force_network or self._network_probe_refresh_due() or not self.network_probe_results
        ):
            self.network_probe_results = probe_targets(
                self.network_probe_targets,
                timeout_seconds=self.network_probe_timeout_seconds,
                latency_warn_ms=self.network_probe_latency_warn_ms,
                tls_warn_days=self.network_probe_tls_warn_days,
            )
            self.network_probe_last_refresh_monotonic = time.monotonic()
            self.network_probe_last_refresh_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _collect_deploy_readiness(self) -> tuple[dict[str, DeploySpec], dict[str, DeployReadinessResult], str]:
        target_names = [target.name for target in self.network_probe_targets]
        deploy_specs = load_deploy_specs(target_names)
        deploy_readiness = probe_deploy_readiness(
            target_names,
            deploy_specs,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return deploy_specs, deploy_readiness, checked_at

    def _refresh_deploy_readiness(self) -> None:
        deploy_specs, deploy_readiness, checked_at = self._collect_deploy_readiness()
        self.deploy_specs = deploy_specs
        self.deploy_readiness = deploy_readiness
        self.deploy_readiness_last_refresh_at = checked_at

    def _start_deploy_readiness_refresh(self, reason: str) -> None:
        if self.deploy_readiness_loading:
            return
        self.deploy_readiness_loading = True
        self.deploy_readiness_spinner_index = 0
        self.deploy_readiness_seq += 1
        seq = self.deploy_readiness_seq
        self._set_job_status("DEPLOY PREFLIGHT RUNNING...", render_now=False)
        self._set_status(f"{reason}: DEPLOY PREFLIGHT RUNNING...", "warn")
        self._render()
        self.refresh()
        threading.Thread(
            target=self._deploy_readiness_worker,
            args=(seq,),
            daemon=True,
        ).start()

    def _deploy_readiness_worker(self, seq: int) -> None:
        try:
            deploy_specs, deploy_readiness, checked_at = self._collect_deploy_readiness()
            self.call_from_thread(
                self._finish_deploy_readiness_refresh,
                seq,
                deploy_specs,
                deploy_readiness,
                checked_at,
                "",
            )
        except Exception as exc:
            self.call_from_thread(
                self._finish_deploy_readiness_refresh,
                seq,
                None,
                None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(exc),
            )

    def _finish_deploy_readiness_refresh(
        self,
        seq: int,
        deploy_specs: dict[str, DeploySpec] | None,
        deploy_readiness: dict[str, DeployReadinessResult] | None,
        checked_at: str,
        error: str,
    ) -> None:
        if seq != self.deploy_readiness_seq:
            return
        self.deploy_readiness_loading = False
        if error:
            self._set_job_status("DEPLOY PREFLIGHT FAILED", render_now=False)
            self._set_status(f"DEPLOY PREFLIGHT FAILED: {error}", "error")
            self._render()
            self.refresh()
            return

        self.deploy_specs = deploy_specs or {}
        self.deploy_readiness = deploy_readiness or {}
        self.deploy_readiness_last_refresh_at = checked_at

        fail_count = sum(1 for item in self.deploy_readiness.values() if item.status == "FAIL")
        warn_count = sum(1 for item in self.deploy_readiness.values() if item.status == "WARN")
        if fail_count > 0:
            self._set_job_status("DEPLOY PREFLIGHT NOT READY", render_now=False)
            self._set_status(
                f"DEPLOY PREFLIGHT COMPLETE: {fail_count} FAIL / {warn_count} WARN",
                "warn",
            )
        elif warn_count > 0:
            self._set_job_status("DEPLOY PREFLIGHT WARN", render_now=False)
            self._set_status(
                f"DEPLOY PREFLIGHT COMPLETE: 0 FAIL / {warn_count} WARN",
                "warn",
            )
        else:
            self._set_job_status("DEPLOY PREFLIGHT READY", render_now=False)
            self._set_status("DEPLOY PREFLIGHT COMPLETE: ALL TARGETS READY", "ok")
        self._render()
        self.refresh()

    def _network_probe_refresh_due(self) -> bool:
        if self.network_probe_last_refresh_monotonic <= 0:
            return True
        return (
            time.monotonic() - self.network_probe_last_refresh_monotonic
            >= self.network_probe_interval_seconds
        )

    def _render(self) -> None:
        self._apply_state_theme()
        self._sync_command_label()
        screen = self.query_one("#screen_text", Static)
        screen.update("\n".join(self._screen_lines()))

        operator_strip = self.query_one("#operator_strip", Static)
        operator_strip.update(self._operator_strip_text())

        confirm_bar = self.query_one("#confirm_bar", Static)
        confirm_bar.update(self._confirm_bar_text())

        footer = self.query_one("#pf_footer", Static)
        footer.update(self._pf_footer_text())

        self.query_one("#bottom_rule", Static).update(self._rule("="))

    def _on_clock_tick(self) -> None:
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            if self._service_ttl_remaining_seconds() <= 0:
                self._auto_revert_service_mode("SERVICE TTL EXPIRED. RETURNED TO OBSERVE")
        panel_needs_status_refresh = (
            (self.panel == "1" and self.panel1_mode == "STATUS")
            or self.panel == "I"
        )
        if panel_needs_status_refresh and self._network_probe_refresh_due():
            self._refresh_system_status(force_network=True)
            if self.panel == "I" and self.instance_detail_target:
                self._load_instance_detail(self.instance_detail_target, force=True)
        self._render()

    def _on_spinner_tick(self) -> None:
        if not self.deploy_readiness_loading:
            return
        self.deploy_readiness_spinner_index = (
            self.deploy_readiness_spinner_index + 1
        ) % len(self.deploy_readiness_spinner_frames)
        self._render()

    def _apply_state_theme(self) -> None:
        self.app_state = self._resolve_state()
        structure_color = self._structure_color()

        self.query_one("#screen_text", Static).styles.color = "white"
        self.query_one("#command_label", Static).styles.color = "white"
        command_line = self.query_one("#command_line", Input)
        command_line.styles.color = "white"
        try:
            command_line.styles.caret_color = "white"
        except Exception:
            pass
        operator_strip = self.query_one("#operator_strip", Static)
        operator_strip.styles.color = structure_color
        self.query_one("#pf_footer", Static).styles.color = structure_color
        self.query_one("#bottom_rule", Static).styles.color = structure_color

        confirm_bar = self.query_one("#confirm_bar", Static)
        if self.service_confirm_required or (
            self.authenticated and self.selected_postauth_state == AppState.SERVICE
        ):
            confirm_bar.styles.color = STATE_PALETTES[AppState.SERVICE]["accent"]
        else:
            confirm_bar.styles.color = structure_color

    def _resolve_state(self) -> AppState:
        if not self.authenticated:
            return self.app_state
        return self.selected_postauth_state

    def _screen_lines(self) -> list[str]:
        accent_style = self._accent_style()
        mode_style = self._mode_style()
        if self.panel == "L":
            lines = self._panel_logon_lines()
            lines.append(self._style_text(self._rule("-"), accent_style))
            return lines

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        right_time = self._header_right_time(now)
        right_ttl = self._header_right_ttl()
        right_env = f"ENV: {self._env_banner()}"
        right_col_width = max(len(right_env), len(right_time), len(right_ttl))
        lines = [
            self._line_lr(
                "CASHLY CTL OPERATIONS CONSOLE (cashlyctl console)",
                right_env.rjust(right_col_width),
                left_style=accent_style,
                right_style=accent_style,
            ),
            self._line_lr(
                f"MODE: {self._mode_badge_text(self.app_state)}",
                right_time.rjust(right_col_width),
                left_style=mode_style,
                right_style=accent_style,
            ),
            self._line_lr(
                f"JOB STATUS: {self.job_status}",
                right_ttl.rjust(right_col_width) if right_ttl else "",
                left_style=accent_style,
                right_style=accent_style,
            ),
            self._line_lr(
                f"USER: {self.session_user}",
                "",
                left_style=accent_style,
            ),
            self._style_text(self._rule("="), accent_style),
            "",
        ]

        if self.panel == "0":
            lines.extend(self._panel0_lines())
        elif self.panel == "B":
            lines.extend(self._panel_boot_checks_lines())
        elif self.panel == "4":
            lines.extend(self._panel4_lines())
        elif self.panel == "7":
            lines.extend(self._panel7_deployments_lines())
        elif self.panel == "1":
            lines.extend(self._panel1_lines())
        elif self.panel == "I":
            lines.extend(self._panel_instance_detail_lines())
        elif self.panel == "2":
            lines.extend(self._panel2_lines())
        elif self.panel == "P":
            lines.extend(self._profile_panel_lines())
        elif self.panel in self.MENU_ITEMS:
            lines.extend(self._placeholder_panel_lines())
        else:
            lines.extend(["INVALID PANEL", ""])

        lines.append(self._style_text(self._rule("-"), accent_style))
        return lines

    def _panel_logon_lines(self) -> list[str]:
        logo_lines = _cashly_ctl_art(self._width(), self.logo_text, self.logo_font)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        right_time = self._header_right_time(current_time)
        right_ttl = self._header_right_ttl()
        right_ip = f"{self.host_ip_label} = {self.host_ip}"
        right_col_width = max(len(right_ip), len(right_time), len(right_ttl))
        accent_style = self._accent_style()
        logo_style = self._mode_style()
        styled_logo_lines = [self._style_text(line, logo_style) for line in logo_lines]
        header_state = (
            self.selected_postauth_state.value
            if self.target_state_selected
            else AppState.PREAUTH.value
        )
        header_mode = self.selected_postauth_state if self.target_state_selected else AppState.PREAUTH
        return [
            self._line_lr(
                "CASHLYCTL OPERATIONS CONSOLE 0.1",
                right_ip.rjust(right_col_width),
                left_style=accent_style,
                right_style=accent_style,
            ),
            self._line_lr(
                f"MODE: {self._mode_badge_text(header_mode)}",
                right_time.rjust(right_col_width),
                left_style=self._mode_style(header_mode),
                right_style=accent_style,
            ),
            self._line_lr(
                f"JOB STATUS: {self.job_status}",
                right_ttl.rjust(right_col_width) if right_ttl else "",
                left_style=accent_style,
                right_style=accent_style,
            ),
            "",
            "",
            self._center("Welcome to CashlyCTL Application Control System v0.1"),
            "",
            *styled_logo_lines,
            "",
            self._center("System Customization - Cashly Inc.*"),
            "",
            '===> Enter "LOGON" to start sign-on sequence',
            "===> Step 1: enter USERID",
            "===> Step 2: enter PASSWORD (masked)",
            "===> Inline password input is ignored for safety",
            "",
            "===> Select target postauth state before logon:",
            "     1 = OBSERVE",
            "     2 = MAINT",
            "     3 = SERVICE (superadmin only)",
            "===> Command: SET STATE <observe|maint|service>",
            "===> Runtime service arm: SERVICE ON, then PROCEED <target>",
            "===> Role policy: admin=OBSERVE/MAINT, superadmin=ALL",
            "",
            self._line_lr(
                f"ENV: {self._env_banner()}",
                f"PROFILE: {self.active_profile.name}",
                left_style=accent_style,
                right_style=accent_style,
            ),
            self._line_lr(
                f"LOCAL USER: {self.os_user}",
                f"SESSION: {self.session_user}",
                left_style=accent_style,
                right_style=accent_style,
            ),
            "",
        ]

    def _accent_style(self) -> str:
        return f"bold {self._structure_color()}"

    def _mode_style(self, state: AppState | None = None) -> str:
        active = state or self.app_state
        return f"bold {STATE_PALETTES[active]['accent']}"

    def _mode_color(self, state: AppState | None = None) -> str:
        active = state or self.app_state
        return STATE_PALETTES[active]["accent"]

    def _structure_color(self) -> str:
        return STATE_PALETTES[self.app_state]["accent"]

    @staticmethod
    def _mode_badge_text(state: AppState) -> str:
        mapping = {
            AppState.PREAUTH: "[ PREAUTH ]",
            AppState.OBSERVE: "[ OBSERVE ]",
            AppState.MAINT: "[ MAINTENANCE ]",
            AppState.SERVICE: "[ SERVICE / WRITE ENABLED ]",
        }
        return mapping[state]

    def _panel_boot_checks_lines(self) -> list[str]:
        right = "RUNNING" if self.boot_checks_running else "READY"
        lines = [
            self._line_lr("PANEL: B  POST-LOGON SYSTEM CHECKS", f"STATE: {right}"),
            self._style_text(self._rule("="), self._accent_style()),
            "",
            "Boot check sequence (stubbed) - live checks will be wired later.",
            "",
        ]
        content = self._boot_content_lines()
        viewport = self._boot_viewport_size()
        if len(content) > viewport:
            max_offset = len(content) - viewport
            self.boot_scroll_offset = min(self.boot_scroll_offset, max_offset)
            start = max(0, len(content) - viewport - self.boot_scroll_offset)
            content = content[start : start + viewport]
        lines.extend(content)
        return lines

    def _start_boot_checks(self) -> None:
        self._stop_boot_checks()
        self.boot_checks_lines = []
        self.boot_checks_sequence = self._build_boot_checks_sequence()
        self.boot_checks_index = 0
        self.boot_checks_running = True
        self.boot_checks_wait_for_enter = False
        self.boot_scroll_offset = 0
        self.panel = "B"
        self._set_job_status("POST-LOGON CHECKS RUNNING", render_now=False)
        self._set_status("RUNNING POST-LOGON SYSTEM CHECKS...", "ok")
        self._render()
        self.refresh()

        self._boot_checks_tick()
        self.boot_checks_timer = self.set_interval(1.0 / 15.0, self._boot_checks_tick)

    def _boot_content_lines(self) -> list[str]:
        content = list(self.boot_checks_lines)
        if self.boot_checks_wait_for_enter:
            content.extend(["", "SYSTEM CHECKS COMPLETE - PRESS ENTER TO CONTINUE", ""])
        return content

    def _boot_viewport_size(self) -> int:
        # Keep room for global header/footer and panel caption lines.
        return max(8, self.size.height - 18)

    def _stop_boot_checks(self) -> None:
        timer = self.boot_checks_timer
        self.boot_checks_timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        self.boot_checks_running = False

    def _boot_checks_tick(self) -> None:
        if not self.boot_checks_running:
            return
        if self.boot_checks_index < len(self.boot_checks_sequence):
            kind, label, status = self.boot_checks_sequence[self.boot_checks_index]
            self.boot_checks_index += 1

            if kind == "phase":
                self.boot_checks_lines.append(self._style_text(label, self._accent_style()))
            else:
                self.boot_checks_lines.append(self._boot_check_line(label, status))

        if self.boot_checks_index >= len(self.boot_checks_sequence):
            self._finish_boot_checks()
            return
        self._render()
        self.refresh()

    def _finish_boot_checks(self) -> None:
        self._stop_boot_checks()
        self.boot_checks_wait_for_enter = True
        self._set_job_status("POST-LOGON CHECKS COMPLETE", render_now=False)
        self._set_status("SYSTEM CHECKS COMPLETE. PRESS ENTER TO CONTINUE", "ok")
        self._render()
        self.refresh()

    def _handle_boot_checks_input(self, raw: str) -> None:
        if raw:
            parsed = parse_command(raw)
            if parsed.kind == CommandKind.LOGOFF:
                audit_command(self.active_profile.name, raw)
                self._handle_logoff()
                return
            if parsed.kind == CommandKind.EXIT:
                audit_command(self.active_profile.name, raw)
                self._handle_exit()
                return

        if self.boot_checks_running:
            self._set_status("SYSTEM CHECKS RUNNING... PLEASE WAIT", "warn")
            return

        if self.boot_checks_wait_for_enter and not raw:
            self.boot_checks_wait_for_enter = False
            self.boot_scroll_offset = 0
            self.panel = "0"
            self.back_stack.clear()
            self._set_job_status("READY", render_now=False)
            self._render()
            self.refresh()
            self._set_status("SYSTEM CHECKS COMPLETE. ENTERED PRIMARY MENU.", "ok")
            return

        if self.boot_checks_wait_for_enter:
            self._set_status("PRESS ENTER TO CONTINUE", "warn")

    def _boot_check_line(self, message: str, status: str) -> str:
        style = {
            "OK": "bold green",
            "WARN": "bold yellow",
            "FAIL": "bold red",
            "SKIP": "bold #aaaaaa",
        }.get(status, self._accent_style())
        return self._line_lr(message, f"[ {status} ]", right_style=style)

    def _build_boot_checks_sequence(self) -> list[tuple[str, str, str]]:
        return [
            ("phase", "-- Identity & Mode --", ""),
            ("check", "Validating auth token audience/expiry", "OK"),
            ("check", "Loading role policy and selected mode", "OK"),
            ("check", "Resolving active profile target", "OK"),
            ("check", "Checking local/server time drift (<5s)", "OK"),
            ("phase", "-- Control Plane --", ""),
            ("check", "Starting control plane reachability (/health)", "OK"),
            ("check", "Verifying control API auth (/session)", "OK"),
            ("check", "Validating client/server contract version", "WARN"),
            ("check", "Loading feature gates (analytics, ops, audit)", "OK"),
            ("phase", "-- Network & DNS --", ""),
            ("check", "Resolving DNS: neo4j, dealsense, metrics", "OK"),
            ("check", "Validating TLS chain and expiry windows", "OK"),
            ("check", "Checking outbound egress policy", "SKIP"),
            ("phase", "-- Core Services --", ""),
            ("check", "DealSense API health endpoint", "OK"),
            ("check", "DealSense model id/version loaded", "OK"),
            ("check", "Inference smoke test (synthetic)", "SKIP"),
            ("check", "Graph service reachable via control plane", "OK"),
            ("check", "Graph read query smoke", "OK"),
            ("check", "Write lock policy in OBSERVE", "OK"),
            ("phase", "-- Observability --", ""),
            ("check", "Metrics endpoint availability", "OK"),
            ("check", "Log export channel availability", "OK"),
            ("check", "Recent error-rate quick read (15m)", "WARN"),
            ("phase", "-- Deployment Integrity --", ""),
            ("check", "Engine version matches expected release", "OK"),
            ("check", "Model checksum vs release manifest", "OK"),
            ("check", "Config fingerprint baseline compare", "WARN"),
            ("phase", "-- Safety Gates --", ""),
            ("check", "Confirming write capability lock", "OK"),
            ("check", "Validating SERVICE arming guard", "OK"),
            ("check", "Validating server-side role constraints", "OK"),
            ("phase", "-- Infrastructure (profile-dependent) --", ""),
            ("check", "Infra connector reachability", "SKIP"),
            ("check", "Proxmox/AWS/Azure capability probe", "SKIP"),
        ]

    def _panel0_lines(self) -> list[str]:
        lines = [
            self._center("PRIMARY OPTION MENU  (Panel 0)"),
            "",
        ]
        for code, label in self.MENU_ITEMS.items():
            if code == "5" and self.active_profile.mode == DeploymentMode.ENTERPRISE:
                lines.append(f"  {code}  {label} (disabled in enterprise)")
                continue

            marker = "*" if code in WRITE_IMPACT_MENU_ITEMS else ""
            line = f"  {code}  {label}{marker}"
            lines.append(line)
        lines.append("")
        return lines

    def _panel1_lines(self) -> list[str]:
        right = (
            "MODE: LOG VIEW"
            if self.panel1_mode == "LOG_VIEW"
            else f"REFRESH: {self.network_probe_interval_seconds}s"
        )
        lines = [
            self._line_lr("PANEL: 1  SYSTEMS STATUS", right),
            self._rule("="),
            "",
        ]
        if self.panel1_mode == "LOG_VIEW":
            lines.extend(self._panel1_log_view_lines())
        else:
            lines.extend(self._panel1_status_lines())
        return lines

    def _panel1_status_lines(self) -> list[str]:
        row_cells = [
            self._network_probe_row_cells(result, idx + 1)
            for idx, result in enumerate(self.network_probe_results)
        ]
        col_widths = self._network_probe_column_widths(row_cells)
        rollup = self._network_rollup_line()
        lines = [
            self._section_header("NETWORK / EDGE LAYER"),
            "",
            rollup,
            "",
            self._network_probe_table_header(col_widths),
            self._rule("-"),
        ]
        if not self.network_probe_results:
            lines.extend(
                [
                    "No probe results yet. Press PF5=Refresh to run network probes.",
                ]
            )
        else:
            for idx, result in enumerate(self.network_probe_results, start=1):
                lines.append(self._network_probe_table_row(result, col_widths, idx))
        lines.extend(
            [
                "",
                f"LAST PROBE REFRESH: {self.network_probe_last_refresh_at}",
                (
                    f"AUTO REFRESH: {self.network_probe_interval_seconds}s"
                    f" | TARGETS: {len(self.network_probe_targets)}"
                ),
                "DRILL DOWN: ENTER ROW NUMBER OR DETAIL <instance_name>",
                "",
                "SERVICES",
                "  NAME        HEALTH   DETAIL",
            ]
        )
        lines.extend(self._service_status_rows())
        lines.extend(
            [
                "",
                "RECENT EVENTS",
                "  network probes run concurrently (DNS/TLS/HTTP) per endpoint",
                "  WARN if 401/403/429/5xx, TLS expiry < 14d, or latency over threshold",
                "  FAIL if DNS or TLS fails, or HTTP times out",
                "",
            ]
        )
        return lines

    def _section_header(self, label: str) -> str:
        prefix = f"-- {label} "
        width = self._width()
        if len(prefix) >= width:
            return prefix[:width]
        return prefix + ("-" * (width - len(prefix)))

    def _network_probe_table_header(self, widths: tuple[int, int, int, int]) -> str:
        name_w, dns_w, tls_w, http_w, origin_w = widths
        return (
            f"{'NAME':<{name_w}} "
            f"{'DNS':<{dns_w}} "
            f"{'TLS':<{tls_w}} "
            f"{'HTTP':<{http_w}} "
            f"{'ORIGIN':<{origin_w}}"
        )

    def _network_probe_table_row(
        self,
        result: NetworkProbeResult,
        widths: tuple[int, int, int, int, int],
        index: int,
    ) -> str:
        name_w, dns_w, tls_w, http_w, origin_w = widths
        name_raw, dns_raw, tls_raw, http_raw, origin_raw = self._network_probe_row_cells(
            result,
            index,
        )
        name_plain = f"{self._clip_cell(name_raw, name_w):<{name_w}}"
        dns_plain = f"{self._clip_cell(dns_raw, dns_w):<{dns_w}}"
        tls_plain = f"{self._clip_cell(tls_raw, tls_w):<{tls_w}}"
        http_plain = f"{self._clip_cell(http_raw, http_w):<{http_w}}"
        origin_plain = f"{self._clip_cell(origin_raw, origin_w):<{origin_w}}"

        name_cell = name_plain
        dns_cell = self._colorize_probe_status_cell(dns_plain)
        tls_cell = self._colorize_probe_status_cell(tls_plain)
        tls_cell = self._colorize_tls_expiry(tls_cell, result.tls_days_left)
        http_cell = self._colorize_probe_http_cell(http_plain, result.http_status)
        origin_cell = origin_plain

        return f"{name_cell} {dns_cell} {tls_cell} {http_cell} {origin_cell}"

    def _network_probe_row_cells(
        self,
        result: NetworkProbeResult,
        index: int,
    ) -> tuple[str, str, str, str, str]:
        name_cell = f"{index:>2}  {result.name}"
        dns_state = "OK" if result.dns_ok else "FAIL"
        dns_cell = f"{dns_state} {result.dns_ms}ms ip={result.dns_ip}"

        if result.tls_ok:
            tls_cell = f"OK {result.tls_ms}ms exp={result.tls_days_left}d"
            if result.tls_cn:
                tls_cell += f" cn={result.tls_cn}"
        else:
            tls_cell = f"FAIL {result.tls_ms}ms"

        http_state = self._http_state(result)
        http_status = str(result.http_status) if result.http_status else "-"
        http_cell = f"{http_state} {result.http_ms}ms {http_status}"
        if result.http_hint and result.http_hint != "-":
            http_cell += f" {result.http_hint}"
        origin_cell = self._origin_display(result)
        return name_cell, dns_cell, tls_cell, http_cell, origin_cell

    def _network_probe_column_widths(
        self,
        rows: list[tuple[str, str, str, str, str]],
    ) -> tuple[int, int, int, int, int]:
        headers = ("NAME", "DNS", "TLS", "HTTP", "ORIGIN")
        desired = [len(header) for header in headers]
        for row in rows:
            for idx in range(5):
                desired[idx] = max(desired[idx], len(row[idx]))

        available = max(40, self._width() - 4)
        total_desired = sum(desired)
        if total_desired <= available:
            return desired[0], desired[1], desired[2], desired[3], desired[4]

        minimum = [
            max(len(headers[0]), 14),
            max(len(headers[1]), 16),
            max(len(headers[2]), 18),
            max(len(headers[3]), 14),
            max(len(headers[4]), 18),
        ]
        widths = desired[:]
        overflow = total_desired - available

        shrink_order = (2, 4, 1, 3, 0)
        while overflow > 0:
            changed = False
            for idx in shrink_order:
                if widths[idx] > minimum[idx]:
                    widths[idx] -= 1
                    overflow -= 1
                    changed = True
                    if overflow == 0:
                        break
            if not changed:
                break

        return widths[0], widths[1], widths[2], widths[3], widths[4]

    @staticmethod
    def _clip_cell(text: str, width: int) -> str:
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."

    @staticmethod
    def _http_state(result: NetworkProbeResult) -> str:
        if not result.http_ok and result.http_status == 0:
            return "FAIL"
        if result.http_status in {401, 403, 429} or result.http_status >= 500:
            return "WARN"
        return "OK"

    def _colorize_probe_status_cell(self, text: str) -> str:
        stripped = text.lstrip()
        leading = text[: len(text) - len(stripped)]
        for status in ("OK", "WARN", "FAIL"):
            if stripped.startswith(status):
                styled = self._style_text(status, self._status_style(status))
                return leading + styled + stripped[len(status) :]
        return text

    def _colorize_probe_http_cell(self, text: str, status_code: int) -> str:
        colored = self._colorize_probe_status_cell(text)
        style = self._http_code_style(status_code)
        if not style or status_code <= 0:
            return colored
        code = str(status_code)
        pattern = rf"(?<![0-9A-Za-z]){re.escape(code)}(?![0-9A-Za-z])"
        return re.sub(
            pattern,
            self._style_text(code, style),
            colored,
            count=1,
        )

    def _colorize_tls_expiry(self, text: str, tls_days_left: int) -> str:
        style = self._tls_expiry_style(tls_days_left)
        if not style:
            return text
        match = re.search(r"exp=(\d+)d", text)
        if not match:
            return text
        token = match.group(0)
        return text.replace(token, self._style_text(token, style), 1)

    @staticmethod
    def _status_style(status: str) -> str:
        normalized = status.upper()
        if normalized == "OK":
            return "bold #00ff00"
        if normalized == "WARN":
            return "bold #ffd700"
        if normalized == "FAIL":
            return "bold #ff3030"
        return "white"

    @staticmethod
    def _http_code_style(status_code: int) -> str | None:
        if status_code <= 0:
            return None
        if 200 <= status_code <= 399:
            return "bold #00ff00"
        if status_code in {401, 403, 429}:
            return "bold #ffd700"
        if status_code >= 500:
            return "bold #ff3030"
        return "bold #ffd700"

    @staticmethod
    def _tls_expiry_style(days_left: int) -> str | None:
        if days_left <= 0:
            return "bold #ff3030"
        if days_left < 14:
            return "bold #ff3030"
        if days_left < 30:
            return "bold #ffd700"
        return "bold #00ff00"

    def _network_rollup_line(self) -> str:
        if not self.network_probe_results:
            return "NETWORK STATUS: UNKNOWN (NO DATA)"

        ok_count = sum(1 for item in self.network_probe_results if item.overall.value == "OK")
        warn_count = sum(1 for item in self.network_probe_results if item.overall.value == "WARN")
        fail_count = sum(1 for item in self.network_probe_results if item.overall.value == "FAIL")
        total = len(self.network_probe_results)

        if fail_count > 0:
            label = self._style_text("IMPACTED", self._status_style("FAIL"))
            return f"NETWORK STATUS: {label} ({fail_count} FAIL)"
        if warn_count > 0:
            label = self._style_text("DEGRADED", self._status_style("WARN"))
            return f"NETWORK STATUS: {label} ({ok_count} OK / {warn_count} WARN)"
        label = self._style_text("HEALTHY", self._status_style("OK"))
        return f"NETWORK STATUS: {label} ({ok_count}/{total} OK)"

    def _origin_display(self, result: NetworkProbeResult) -> str:
        env_key = f"CASHLYCTL_ORIGIN_{re.sub(r'[^A-Za-z0-9]+', '_', result.name).upper()}"
        configured = _runtime_env(env_key, "").strip()
        if configured:
            return configured
        cached = self.instance_detail_cache.get(result.name.lower())
        if cached and cached.origin_ip and cached.origin_ip != "-":
            return cached.origin_ip
        if "cloudflare" in result.http_hint.lower():
            return "hidden-behind-cloudflare"
        return result.dns_ip

    def _panel1_log_view_lines(self) -> list[str]:
        service = self.tail_target
        source = "journalctl -u neo4j" if "neo4j" in service else "journalctl -u dealsense"
        lines = [
            f"TAIL: {service}  (last {self.tail_lines} lines)        SOURCE: {source}",
            self._rule("-"),
        ]
        for item in self._active_logs():
            lines.append(item)
        lines.append(self._rule("-"))
        lines.append("")
        return lines

    def _panel_instance_detail_lines(self) -> list[str]:
        target = self.instance_detail_target or "-"
        detail = self.instance_detail_data
        probe = self._probe_result_for_target(target)
        lines = [
            self._line_lr(
                "PANEL: I  INSTANCE DETAIL",
                f"TARGET: {target}",
            ),
            self._rule("="),
            "",
        ]
        if not target:
            lines.extend(
                [
                    "No instance selected.",
                    "Go to Panel 1 and enter a row number, or use DETAIL <name>.",
                    "",
                ]
            )
            return lines

        if detail is None:
            lines.extend(
                [
                    "Instance detail not loaded.",
                    "Use REFRESH or DETAIL <name>.",
                    "",
                ]
            )
            return lines

        lines.extend(
            [
                self._line_lr(
                    "IDENTITY",
                    f"AWS SDK: {'READY' if self.aws_sdk_ready else 'MISSING'}",
                ),
                self._rule("-"),
                f"  Name:         {detail.identity_name}",
                f"  Instance ID:  {detail.instance_id}",
                f"  State:        {detail.state}",
                f"  AZ:           {detail.availability_zone}",
                f"  Private IP:   {detail.private_ip}",
                f"  Public IP:    {detail.public_ip}",
                f"  Launch Time:  {detail.launch_time}   Uptime: {detail.uptime}",
                f"  Type:         {detail.instance_type}   AMI: {detail.image_id}",
                f"  OS Guess:     {detail.os_guess}",
                "",
                "NETWORK PATH",
                self._rule("-"),
                f"  Cloudflare Hostname: {detail.hostname}",
                f"  ALB Listener:        {detail.alb_listener}",
                f"  Target Group:        {detail.target_group}",
                f"  Target Health:       {detail.target_health}",
                f"  Health Reason:       {detail.target_health_reason}",
                f"  Origin (ALB/Instance): {detail.origin_ip}",
                "",
                "APP HEALTH",
                self._rule("-"),
            ]
        )
        if probe:
            overall_style = self._status_style(probe.overall.value)
            overall_text = self._style_text(probe.overall.value, overall_style)
            http_state = self._http_state(probe)
            http_state_text = self._style_text(http_state, self._status_style(http_state))
            http_code_text = str(probe.http_status) if probe.http_status else "-"
            code_style = self._http_code_style(probe.http_status) or "white"
            lines.extend(
                [
                    f"  Overall:      {overall_text}",
                    f"  DNS:          {probe.dns_ip} ({probe.dns_ms}ms)",
                    f"  TLS:          {probe.tls_ms}ms exp={probe.tls_days_left}d cn={probe.tls_cn or '-'}",
                    (
                        f"  HTTP:         {http_state_text} {probe.http_ms}ms "
                        f"{self._style_text(http_code_text, code_style)}  hint={probe.http_hint}"
                    ),
                ]
            )
        else:
            lines.append("  No HTTP/TLS probe data in current session.")

        system_status = self._style_text(
            detail.status_check_system,
            self._aws_check_style(detail.status_check_system),
        )
        instance_status = self._style_text(
            detail.status_check_instance,
            self._aws_check_style(detail.status_check_instance),
        )
        lines.extend(
            [
                "",
                "COMPUTE",
                self._rule("-"),
                f"  CloudWatch CPU (15m avg): {detail.cpu_avg_15m}",
                f"  EC2 Status Checks: system={system_status} instance={instance_status}",
                "",
                "LOGS",
                self._rule("-"),
                "  Open recent nginx logs:    TODO (SSM session integration)",
                "  Open recent application logs: TODO (SSM / CW logs integration)",
                "",
                "ACTIONS",
                self._rule("-"),
            ]
        )

        if self.selected_postauth_state == AppState.OBSERVE:
            lines.append("  OBSERVE mode: view-only.")
        else:
            lines.append("  MAINT/SERVICE: restart/deregister actions planned (gated confirmation).")

        lines.extend(
            [
                "",
                f"LAST DETAIL REFRESH: {self.instance_detail_last_refresh_at}",
            ]
        )
        if detail.error:
            lines.append(f"AWS NOTE: {detail.error}")
        if detail.source_note:
            lines.append(f"AWS HINT: {detail.source_note}")
        lines.extend(
            [
                "",
                "Use PF3=Back to return to Panel 1. Use DETAIL <name> or REFRESH to reload.",
                "",
            ]
        )
        return lines

    def _probe_result_for_target(self, target_name: str) -> NetworkProbeResult | None:
        lookup = target_name.strip().lower()
        for item in self.network_probe_results:
            if item.name.lower() == lookup:
                return item
        return None

    @staticmethod
    def _aws_check_style(status: str) -> str:
        value = status.strip().lower()
        if value == "ok":
            return "bold #00ff00"
        if value in {"impaired", "insufficient-data"}:
            return "bold #ffd700"
        if value == "-":
            return "white"
        return "bold #ff3030"

    def _panel2_lines(self) -> list[str]:
        lines = [
            self._line_lr("PANEL: 2  NEO4J CONSOLE", "DB: neo4j"),
            self._rule("="),
            "",
        ]
        if self.panel2_mode == "SAVED" and self.saved_query:
            lines.extend(
                [
                    "SAVED:",
                    f"  QRY: {self.saved_query[0]}",
                    f"  PATH: {self.saved_query[1]}",
                    "  PARAMS: saved (redacted secrets)",
                    "",
                ]
            )
            return lines

        lines.extend(
            [
                "QUERY EDITOR  (multi-line; end with RUN or PF9)",
                self._rule("-"),
            ]
        )
        lines.extend(self.neo4j_query.splitlines())
        lines.extend(
            [
                self._rule("-"),
                f"PARAMS: {self.neo4j_params}",
                "",
                "RESULTS (table)                               STATS: rows=1  time=84ms",
                self._rule("-"),
                "lender   deals_30d   avg_ltv   avg_dealsense_score",
                "Hosper   118         71.2      0.63",
                self._rule("-"),
                "",
            ]
        )
        return lines

    def _panel4_lines(self) -> list[str]:
        session_text = self.jobs_session_id or "NONE"
        lines = [
            self._line_lr("PANEL: 4  JOBS / RUNS", f"SESSION: {session_text}"),
            self._rule("="),
            "",
        ]
        if not self.authenticated:
            lines.extend(
                [
                    "No authenticated session.",
                    "Log on to create a jobs session log.",
                    "",
                ]
            )
            return lines

        records = self._load_session_jobs()
        lines.extend(
            [
                f"SESSION STARTED: {self.jobs_session_started_at or '-'}",
                f"LOG FILE: {self._jobs_log_display_path()}",
                "",
                "TIMESTAMP            STATUS  EVENT          DETAIL",
                self._rule("-"),
            ]
        )
        for record in records[-20:]:
            lines.append(
                f"{record.get('timestamp', '-'):<20} "
                f"{record.get('status', '-'):6} "
                f"{record.get('event', '-'):<14} "
                f"{record.get('detail', '-')}"
            )
        lines.extend(["", "Showing last 20 jobs for this session.", ""])
        return lines

    def _panel7_deployments_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        targets = [target.name for target in self.network_probe_targets]
        readiness_items = [
            self.deploy_readiness.get(target_name.lower())
            for target_name in targets
            if target_name.strip()
        ]
        ready_count = sum(1 for item in readiness_items if item and item.status == "OK")
        warn_count = sum(1 for item in readiness_items if item and item.status == "WARN")
        fail_count = sum(1 for item in readiness_items if item and item.status == "FAIL")
        unknown_count = len(readiness_items) - ready_count - warn_count - fail_count

        if fail_count > 0:
            rollup_label = "NOT READY"
            rollup_style = "bold #ff3030"
        elif warn_count > 0:
            rollup_label = "READY WITH WARNINGS"
            rollup_style = "bold #ffd700"
        elif ready_count > 0 and unknown_count == 0:
            rollup_label = "READY"
            rollup_style = "bold #00ff00"
        else:
            rollup_label = "UNKNOWN"
            rollup_style = "white"

        precheck_rows = [
            self._deploy_precheck_cells(target_name)
            for target_name in targets
        ]
        precheck_widths = self._deploy_precheck_column_widths(precheck_rows)

        lines = [
            self._line_lr("PANEL: 7  DEPLOYMENTS", f"MODE: {state_mode}"),
            self._rule("="),
            "",
        ]
        if self.deploy_readiness_loading:
            spinner = self.deploy_readiness_spinner_frames[
                self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
            ]
            lines.extend(
                [
                    self._style_text(
                        f"  PREFLIGHT RUNNING [{spinner}] checking SSH/auth/PM2/git...",
                        "bold #ffd700",
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "  1  Deploy CRM Dev",
                "  2  Deploy CRM Prod",
                "  3  Deploy n8n",
                "  4  Rollback",
                "  5  Deploy History",
                "  6  Release Management (later)",
                "",
                "COMMANDS:",
                "  DEPLOY crm-dev",
                "  DEPLOY crm-prod REV <sha>",
                "  DEPLOY crm-prod TAG <tag>",
                "  ROLLBACK crm-prod --to last-good",
                "  STATUS DEPLOY crm-prod",
                "  DIFF crm-prod --current --target <sha>",
                "  PLAN show crm-prod",
                "",
                "DEPLOY PRECHECK STATUS",
                self._rule("-"),
                self._style_text(
                    f"  OVERALL: {rollup_label} "
                    f"({ready_count} READY / {warn_count} WARN / {fail_count} FAIL / {unknown_count} UNKNOWN)",
                    rollup_style,
                ),
                f"  LAST PREFLIGHT: {self.deploy_readiness_last_refresh_at}",
                self._deploy_precheck_header(precheck_widths),
                self._rule("-"),
            ]
        )
        for row in precheck_rows:
            lines.append(self._deploy_precheck_row(row, precheck_widths))
        lines.extend(
            [
                "",
                "  PF5 refreshes readiness checks. PLAN show <target> reruns checks for one target.",
                "",
            ]
        )
        if self.selected_postauth_state == AppState.OBSERVE:
            lines.extend(
                [
                    "POLICY:",
                    "  OBSERVE = view-only (PLAN / STATUS / DIFF only)",
                    "  MAINT/SERVICE = deploy and rollback allowed",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "POLICY:",
                    "  WRITE MODE ACTIVE (MAINT/SERVICE)",
                    "  PROD TARGETS REQUIRE SERVICE MODE",
                    "",
                ]
            )

        lines.append("RECENT DEPLOY HISTORY")
        lines.append(self._rule("-"))
        if not self.deploy_history:
            lines.extend(["  No deployment runs in this session.", ""])
        else:
            lines.append("  FINISHED AT          ACTION    TARGET                    STATUS  REF")
            for item in self.deploy_history[-12:]:
                lines.append(
                    f"  {item.get('finished_at', '-'):<20} "
                    f"{item.get('action', '-'):<9} "
                    f"{item.get('target', '-'):<25} "
                    f"{item.get('status', '-'):<6} "
                    f"{item.get('ref', '-')}"
                )
            lines.append("")

        if self.last_deploy_report:
            lines.append("LAST RUN STEPS")
            lines.append(self._rule("-"))
            for step in self.last_deploy_report.steps[-8:]:
                lines.append(f"  {step.name:<18} [{step.status}] {step.detail[:120]}")
            lines.append("")
        return lines

    def _deploy_mode_gate_state(self, target: str) -> str:
        if self.selected_postauth_state == AppState.OBSERVE:
            return "VIEW"
        if self._target_requires_service(target) and self.selected_postauth_state != AppState.SERVICE:
            return "BLOCK"
        return "ALLOW"

    def _readiness_check_status(
        self,
        readiness: DeployReadinessResult | None,
        check_name: str,
    ) -> str:
        if not readiness:
            return "UNKNOWN"
        for check in readiness.checks:
            if check.name == check_name:
                return check.status
        return "-"

    def _deploy_readiness_summary(self, readiness: DeployReadinessResult | None) -> str:
        if not readiness:
            return "No checks run yet"
        fail_step = next((step for step in readiness.checks if step.status == "FAIL"), None)
        if fail_step:
            return f"{fail_step.name}: {fail_step.detail}"
        warn_step = next((step for step in readiness.checks if step.status == "WARN"), None)
        if warn_step:
            return f"{warn_step.name}: {warn_step.detail}"
        return "All critical checks passing"

    def _deploy_status_token(self, status: str, width: int = 7) -> str:
        token = (status or "-").strip().upper()
        plain = f"{self._clip_cell(token, width):<{width}}"
        style = "white"
        if token in {"OK", "READY", "ALLOW"}:
            style = "bold #00ff00"
        elif token in {"WARN", "VIEW"}:
            style = "bold #ffd700"
        elif token in {"FAIL", "BLOCK"}:
            style = "bold #ff3030"
        elif token in {"UNKNOWN", "-"}:
            style = "white"
        return self._style_text(plain, style)

    def _deploy_precheck_cells(self, target_name: str) -> tuple[str, str, str, str, str, str, str, str, str]:
        readiness = self.deploy_readiness.get(target_name.lower())
        mode_gate = "SERVICE" if self._target_requires_service(target_name) else "MAINT/SVC"
        mode_state = self._deploy_mode_gate_state(target_name)
        summary = self._deploy_readiness_summary(readiness)
        return (
            target_name,
            readiness.status if readiness else "UNKNOWN",
            mode_gate,
            mode_state,
            self._readiness_check_status(readiness, "SSH_KEY"),
            self._readiness_check_status(readiness, "SSH_CONNECT"),
            self._readiness_check_status(readiness, "PM2"),
            self._readiness_check_status(readiness, "GIT_REMOTE"),
            summary,
        )

    def _deploy_precheck_column_widths(
        self,
        rows: list[tuple[str, str, str, str, str, str, str, str, str]],
    ) -> tuple[int, int, int, int, int, int, int, int, int]:
        headers = (
            "TARGET",
            "READY",
            "MODE-GATE",
            "GATE-NOW",
            "KEY",
            "SSH",
            "PM2",
            "GIT",
            "SUMMARY",
        )
        desired = [len(header) for header in headers]
        for row in rows:
            for idx in range(9):
                desired[idx] = max(desired[idx], len(row[idx]))

        available = max(72, self._width() - 2)
        spaces = 8
        total_desired = sum(desired) + spaces
        if total_desired <= available:
            return (
                desired[0],
                desired[1],
                desired[2],
                desired[3],
                desired[4],
                desired[5],
                desired[6],
                desired[7],
                desired[8],
            )

        minimum = [12, 5, 8, 8, 3, 3, 3, 3, 16]
        widths = desired[:]
        overflow = total_desired - available
        shrink_order = (8, 0, 2, 3, 1, 4, 5, 6, 7)

        while overflow > 0:
            changed = False
            for idx in shrink_order:
                if widths[idx] > minimum[idx]:
                    widths[idx] -= 1
                    overflow -= 1
                    changed = True
                    if overflow == 0:
                        break
            if not changed:
                break

        return (
            widths[0],
            widths[1],
            widths[2],
            widths[3],
            widths[4],
            widths[5],
            widths[6],
            widths[7],
            widths[8],
        )

    def _deploy_precheck_header(
        self,
        widths: tuple[int, int, int, int, int, int, int, int, int],
    ) -> str:
        target_w, ready_w, gate_w, gate_now_w, key_w, ssh_w, pm2_w, git_w, summary_w = widths
        return (
            f"  {'TARGET':<{target_w}} {'READY':<{ready_w}} {'MODE-GATE':<{gate_w}} "
            f"{'GATE-NOW':<{gate_now_w}} {'KEY':<{key_w}} {'SSH':<{ssh_w}} "
            f"{'PM2':<{pm2_w}} {'GIT':<{git_w}} {'SUMMARY':<{summary_w}}"
        )

    def _deploy_precheck_row(
        self,
        row: tuple[str, str, str, str, str, str, str, str, str],
        widths: tuple[int, int, int, int, int, int, int, int, int],
    ) -> str:
        target_w, ready_w, gate_w, gate_now_w, key_w, ssh_w, pm2_w, git_w, summary_w = widths
        target_cell = f"{self._clip_cell(row[0], target_w):<{target_w}}"
        ready_cell = self._deploy_status_token(row[1], ready_w)
        mode_gate_cell = f"{self._clip_cell(row[2], gate_w):<{gate_w}}"
        gate_now_cell = self._deploy_status_token(row[3], gate_now_w)
        key_cell = self._deploy_status_token(row[4], key_w)
        ssh_cell = self._deploy_status_token(row[5], ssh_w)
        pm2_cell = self._deploy_status_token(row[6], pm2_w)
        git_cell = self._deploy_status_token(row[7], git_w)
        summary_plain = f"{self._clip_cell(row[8], summary_w):<{summary_w}}"
        summary_cell = self._markup_safe(summary_plain)
        return (
            f"  {target_cell} {ready_cell} {mode_gate_cell} {gate_now_cell} "
            f"{key_cell} {ssh_cell} {pm2_cell} {git_cell} {summary_cell}"
        )

    def _profile_panel_lines(self) -> list[str]:
        state = "LOGGED ON" if self.authenticated else "NOT LOGGED ON"
        lines = [
            self._line_lr("PANEL: P  PROFILE PICKER", f"STATE: {state}"),
            self._rule("="),
            "",
            "SELECT PROFILE:",
            "",
        ]
        for idx, profile in enumerate(self.config.profiles, start=1):
            marker = "*" if profile.name == self.active_profile.name else " "
            lines.append(
                f"  {idx}. [{marker}] {profile.name:<20} "
                f"env={profile.env.value:<5} mode={profile.mode.value}"
            )
        lines.extend(["", "Use number or SET ENV <profile_name>.", ""])
        return lines

    def _placeholder_panel_lines(self) -> list[str]:
        label = self.MENU_ITEMS[self.panel]
        lines = [
            self._line_lr(f"PANEL: {self.panel}  {label.upper()}", ""),
            self._rule("="),
            "",
            f"{label}",
            "",
            "Template placeholder panel.",
            "Use PF3=Back or =0 for the Primary Option Menu.",
            "",
        ]
        return lines

    def _service_status_rows(self) -> list[str]:
        by_name = {item.name.lower(): item for item in self.health_results}
        rows: list[str] = []
        neo4j = by_name.get("neo4j")
        dealsense = by_name.get("dealsense")
        control_api = by_name.get("control_api")
        if neo4j:
            neo4j_health = self._colorize_probe_status_cell(f"{neo4j.status.value:<6}")
            rows.append(
                "  NEO4J       "
                f"{neo4j_health} "
                f"{(self.active_profile.neo4j_bolt_uri or '-').ljust(30)} latency={neo4j.latency_ms}ms"
            )
        if dealsense:
            dealsense_health = self._colorize_probe_status_cell(f"{dealsense.status.value:<6}")
            rows.append(
                "  DEALSENSE   "
                f"{dealsense_health} "
                f"{((self.active_profile.dealsense_url or '-') + '/health').ljust(30)} "
                f"latency={dealsense.latency_ms}ms"
            )
        if control_api and self.active_profile.mode == DeploymentMode.ENTERPRISE:
            control_health = self._colorize_probe_status_cell(f"{control_api.status.value:<6}")
            rows.append(
                "  CONTROL_API "
                f"{control_health} "
                f"{self.active_profile.control_api_base_url.ljust(30)} latency={control_api.latency_ms}ms"
            )
        return rows

    def _active_logs(self) -> list[str]:
        all_logs = _mock_logs(self.tail_target)
        if self.tail_lines < len(all_logs):
            start = len(all_logs) - self.tail_lines
            end = len(all_logs)
            window = all_logs[start:end]
        else:
            window = list(all_logs)
        if self.log_scroll_offset > 0:
            offset = min(self.log_scroll_offset, len(all_logs) - 1)
            start = max(0, len(all_logs) - self.tail_lines - offset)
            end = max(start + 1, len(all_logs) - offset)
            window = all_logs[start:end]
        return window

    def _pf_footer_text(self) -> str:
        pf9_action = "Run" if self.panel == "2" else "Cmd"
        return (
            f"PF1=Help  PF3=Back  PF4=Exit  PF5=Refresh  PF7=Up  PF8=Down  "
            f"PF9={pf9_action}  PF12=Cancel"
        )

    def _operator_strip_text(self) -> str:
        target = self._env_banner()
        role = self.session_role.upper()
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            ttl = _format_ttl(self._service_ttl_remaining_seconds())
            scope = self._scope_indicator()
            strip = f"MODE: SERVICE (WRITE ENABLED) | TTL: {ttl} | SCOPE: {scope}"
            if self._is_broad_scope():
                strip += " | WARNING: BROAD SCOPE"
            return strip
        return (
            f"MODE: {self.app_state.value} | ROLE: {role} | "
            f"PROFILE: {self.active_profile.name} | TARGET: {target}"
        )

    def _header_right_time(self, timestamp: str) -> str:
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            return f"TIME: {timestamp}"
        return f"SYSTEM TIME = {timestamp}"

    def _header_right_ttl(self) -> str:
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            ttl = _format_ttl(self._service_ttl_remaining_seconds())
            return f"TTL: {ttl}"
        return ""

    def _confirm_bar_text(self) -> str:
        if self.service_confirm_required:
            return f"CONFIRM SERVICE: TYPE PROCEED {self.service_confirm_scope}"
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            ttl = _format_ttl(self._service_ttl_remaining_seconds())
            return f"SERVICE WRITE WINDOW ACTIVE | TTL {ttl} | SET STATE OBSERVE TO DISARM"
        return ""

    def _service_ttl_remaining_seconds(self) -> int:
        elapsed = int(max(0.0, time.monotonic() - self.service_last_activity_ts))
        return max(0, self.service_idle_ttl_seconds - elapsed)

    def _service_scope_token(self) -> str:
        return self._env_banner().upper()

    def _scope_indicator(self) -> str:
        env_scope = self._env_banner().lower()
        if self.active_profile.mode == DeploymentMode.ENTERPRISE:
            return f"{env_scope} / control-api"
        return f"{env_scope} / dealsense-dev / neo4j-dev"

    def _is_broad_scope(self) -> bool:
        return (
            self.active_profile.env == Environment.PROD
            or self.active_profile.mode == DeploymentMode.ENTERPRISE
        )

    def _record_activity(self) -> None:
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            self.service_last_activity_ts = time.monotonic()

    def _sync_command_label(self) -> None:
        if self.logon_stage != LogonFlowStage.NONE:
            return
        if self.deploy_readiness_loading:
            self._set_command_label(self._loading_command_label())
            return
        self._set_command_label(self._default_command_label())

    def _command_prefix(self) -> str:
        if self.authenticated and self.selected_postauth_state == AppState.SERVICE:
            return "Command (WRITE)"
        if self.authenticated and self.selected_postauth_state == AppState.MAINT:
            return "Command (MAINT)"
        return "Command"

    def _default_command_label(self) -> str:
        return f"{self._command_prefix()} ===>"

    def _loading_command_label(self) -> str:
        frame = self.deploy_readiness_spinner_frames[
            self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
        ]
        return f"{self._command_prefix()} [PRECHECK {frame}] ===>"

    def _set_command_mode(
        self,
        label: str | None = None,
        password: bool = False,
        placeholder: str = "",
    ) -> None:
        input_widget = self.query_one("#command_line", Input)
        if label is None:
            label = self._default_command_label()
        self._set_command_label(label)
        input_widget.password = password
        input_widget.placeholder = placeholder
        input_widget.value = ""
        input_widget.focus()

    def _set_command_label(self, label: str) -> None:
        label_widget = self.query_one("#command_label", Static)
        label_widget.update(label)
        try:
            label_widget.styles.width = len(label)
        except Exception:
            pass

    @staticmethod
    def _redacted_logon_audit(logon_args: str) -> str:
        args = logon_args.strip()
        if not args:
            return "LOGON"
        parts = args.split()
        if len(parts) == 1:
            return f"LOGON {parts[0]}"
        return f"LOGON {parts[0]} ****"

    def _set_status(self, text: str, level: str) -> None:
        status = self.query_one("#status_line", Static)
        status.update(text)
        for cls in ("ok", "warn", "error"):
            status.remove_class(cls)
        status.add_class(level)

    def _set_job_status(self, text: str, render_now: bool = True) -> None:
        self.job_status = text
        level = "INFO"
        if "FAILED" in text.upper() or "DENIED" in text.upper():
            level = "FAIL"
        elif "WARN" in text.upper() or "PENDING" in text.upper():
            level = "WARN"
        elif "COMPLETE" in text.upper() or "READY" in text.upper() or "ACTIVE" in text.upper():
            level = "OK"
        self._record_session_job("JOB_STATUS", text, level)
        if render_now:
            self._render()
            self.refresh()

    def _start_jobs_session(self, user: str, role: str, mode: AppState) -> None:
        ensure_state_layout()
        sessions_dir = STATE_DIR / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.jobs_session_id = f"{user.lower()}-{stamp}"
        self.jobs_session_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.jobs_session_file = sessions_dir / f"{self.jobs_session_id}.jsonl"
        self.jobs_records = []
        self._record_session_job(
            "SESSION_START",
            f"user={user} role={role} mode={mode.value}",
            "OK",
        )

    def _record_session_job(self, event: str, detail: str, status: str = "INFO") -> None:
        if not self.jobs_session_file:
            return
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "event": event,
            "detail": detail,
        }
        self.jobs_records.append(record)
        try:
            with self.jobs_session_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError:
            pass

    def _load_session_jobs(self) -> list[dict[str, str]]:
        if not self.jobs_session_file or not self.jobs_session_file.exists():
            return list(self.jobs_records)
        loaded: list[dict[str, str]] = []
        try:
            with self.jobs_session_file.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        loaded.append(
                            {
                                "timestamp": str(item.get("timestamp", "")),
                                "status": str(item.get("status", "")),
                                "event": str(item.get("event", "")),
                                "detail": str(item.get("detail", "")),
                            }
                        )
        except OSError:
            return list(self.jobs_records)
        self.jobs_records = loaded
        return loaded

    def _jobs_log_display_path(self) -> str:
        if not self.jobs_session_file:
            return "-"
        try:
            rel = self.jobs_session_file.relative_to(STATE_DIR)
            return f"~/.cashlyctl/state/{str(rel).replace(os.sep, '/')}"
        except ValueError:
            return str(self.jobs_session_file)

    def _env_banner(self) -> str:
        env = self.active_profile.env.value.upper()
        scope = "ONPREM" if self.active_profile.mode == DeploymentMode.INTERNAL else "ENTERPRISE"
        return f"{env}-{scope}"

    def _line_lr(
        self,
        left: str,
        right: str,
        left_style: str | None = None,
        right_style: str | None = None,
    ) -> str:
        width = self._width()
        if not right:
            return self._style_text(left[:width], left_style)
        gap = width - len(left) - len(right)
        if gap < 1:
            safe_left = left[: max(0, width - len(right) - 1)]
            return (
                f"{self._style_text(safe_left, left_style)} "
                f"{self._style_text(right, right_style)}"
            )
        return (
            f"{self._style_text(left, left_style)}"
            f"{' ' * gap}"
            f"{self._style_text(right, right_style)}"
        )

    def _center(self, text: str) -> str:
        return text.center(self._width())

    def _rule(self, char: str) -> str:
        return char * self._width()

    def _width(self) -> int:
        return max(72, self.size.width - 2)

    @staticmethod
    def _style_text(text: str, style: str | None) -> str:
        safe_text = CashlyConsoleApp._markup_safe(text)
        if not style:
            return safe_text
        return f"[{style}]{safe_text}[/]"

    @staticmethod
    def _markup_safe(text: str) -> str:
        return text.replace("[", r"\[")


def _tail_target_label(raw: str) -> str:
    if raw in {"neo4j", "neo4j-dev"}:
        return "neo4j-dev"
    if raw in {"dealsense", "dealsense-dev"}:
        return "dealsense-dev"
    return raw


def _default_network_probe_targets() -> list[NetworkProbeTarget]:
    targets = _network_targets_from_compact_env()
    if targets:
        return targets
    return _network_targets_from_indexed_env()


def _network_targets_from_compact_env() -> list[NetworkProbeTarget]:
    # Format:
    #   CASHLYCTL_NETWORK_TARGETS=n8n-server=https://n8n-aws.gocashly.io,crm-dev=https://crm-development.gocashly.io
    raw = _runtime_env("CASHLYCTL_NETWORK_TARGETS", "").strip()
    if not raw:
        return []
    targets: list[NetworkProbeTarget] = []
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        if "=" not in entry:
            continue
        name, url = entry.split("=", 1)
        probe_name = name.strip()
        probe_url = url.strip()
        if not probe_name or not probe_url:
            continue
        targets.append(NetworkProbeTarget(name=probe_name, url=probe_url))
    return targets


def _network_targets_from_indexed_env(limit: int = 32) -> list[NetworkProbeTarget]:
    # Format:
    #   CASHLYCTL_TARGET_1_NAME=n8n-server
    #   CASHLYCTL_TARGET_1_URL=https://n8n-aws.gocashly.io
    targets: list[NetworkProbeTarget] = []
    for idx in range(1, limit + 1):
        name = _runtime_env(f"CASHLYCTL_TARGET_{idx}_NAME", "").strip()
        url = _runtime_env(f"CASHLYCTL_TARGET_{idx}_URL", "").strip()
        if not name and not url:
            continue
        if not name or not url:
            continue
        targets.append(NetworkProbeTarget(name=name, url=url))
    return targets


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
        key, value = line.split("=", 1)
        env_key = key.strip()
        env_value = value.strip().strip('"').strip("'")
        if env_key and env_value:
            data[env_key] = env_value

    _RUNTIME_ENV_CACHE = data
    return data


def _network_probe_timeout_seconds() -> float:
    raw = _runtime_env("CASHLYCTL_PROBE_TIMEOUT_SEC", "3.0").strip()
    try:
        value = float(raw)
    except ValueError:
        return 3.0
    return max(0.5, min(value, 10.0))


def _network_probe_latency_warn_ms() -> int:
    raw = _runtime_env("CASHLYCTL_PROBE_WARN_LATENCY_MS", "600").strip()
    try:
        value = int(raw)
    except ValueError:
        return 600
    return max(100, min(value, 10_000))


def _service_idle_ttl_seconds() -> int:
    raw = _runtime_env("CASHLYCTL_SERVICE_IDLE_TTL_SEC", "900").strip()
    try:
        value = int(raw)
    except ValueError:
        return 900
    return max(60, value)


def _deploy_readiness_timeout_seconds() -> int:
    raw = _runtime_env("CASHLYCTL_DEPLOY_PREFLIGHT_TIMEOUT_SEC", "10").strip()
    try:
        value = int(float(raw))
    except ValueError:
        return 10
    return max(4, min(value, 30))


def _format_ttl(seconds: int) -> str:
    total = max(0, seconds)
    mins = total // 60
    secs = total % 60
    return f"{mins:02d}:{secs:02d}"


def _mock_logs(service: str) -> list[str]:
    if "neo4j" in service:
        return [
            "Feb 14 10:54:01 neo4j[2213]: Query started: MATCH (d:Deal)-[:SIMILAR_TO]->(d2) ...",
            "Feb 14 10:54:03 neo4j[2213]: Query completed in 2401 ms (db=neo4j, user=ops)",
            "Feb 14 11:01:55 neo4j[2213]: Bolt enabled on 0.0.0.0:7687",
            "Feb 14 11:02:10 neo4j[2213]: CHECKPOINT completed (took 0.9s)",
            "Feb 14 11:02:18 neo4j[2213]: INFO  keepalive ok",
            "Feb 14 11:03:27 neo4j[2213]: INFO  tx throughput=182/s",
            "Feb 14 11:04:03 neo4j[2213]: WARN  query cache eviction=12",
            "Feb 14 11:04:51 neo4j[2213]: INFO  checkpoint scheduled",
        ]
    return [
        "Feb 14 10:58:11 dealsense[772]: INFO  model=deal_ranker v=3.2.1 loaded",
        "Feb 14 11:00:02 dealsense[772]: INFO  /health ok latency=22ms",
        "Feb 14 11:01:18 dealsense[772]: INFO  infer request lender=Hosper ms=305",
        "Feb 14 11:02:18 dealsense[772]: INFO  infer request lender=Hosper ms=312",
        "Feb 14 11:03:44 dealsense[772]: INFO  queue depth=4",
    ]


def _normalize_query_name(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip()).strip("_")
    return slug.lower()


def _query_profile_dir(profile: Profile) -> str:
    suffix = "onprem" if profile.mode == DeploymentMode.INTERNAL else "enterprise"
    return f"{profile.env.value}-{suffix}"


def _state_from_text(raw: str) -> AppState | None:
    value = raw.strip().lower()
    mapping = {
        "observe": AppState.OBSERVE,
        "obs": AppState.OBSERVE,
        "maint": AppState.MAINT,
        "maintenance": AppState.MAINT,
        "service": AppState.SERVICE,
        "svc": AppState.SERVICE,
    }
    return mapping.get(value)


def _detect_banner_ip() -> tuple[str, str]:
    manual_ip = os.getenv("CASHLYCTL_IP", "").strip()
    if manual_ip:
        return ("YOUR IP IS", manual_ip)

    public_ip = _detect_public_ipv4()
    if public_ip:
        return ("YOUR IP IS", public_ip)

    local_ip = _detect_local_ipv4()
    if local_ip:
        return ("YOUR IP IS", local_ip)

    return ("YOUR IP IS", "N/A")


def _detect_public_ipv4(timeout_seconds: float = 1.5) -> str | None:
    custom_endpoint = os.getenv("CASHLYCTL_IP_ENDPOINT", "").strip()
    endpoints: list[str] = []
    if custom_endpoint:
        endpoints.append(custom_endpoint)
    endpoints.extend(
        [
            "https://api.ipify.org",
            "https://ipv4.icanhazip.com",
            "https://ifconfig.me/ip",
        ]
    )

    for endpoint in endpoints:
        try:
            request = urllib.request.Request(
                endpoint, headers={"User-Agent": "cashlyctl/0.1"}
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(128).decode("utf-8", "ignore")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue

        ip = _extract_ipv4(body)
        if ip and _is_public_ipv4(ip):
            return ip
    return None


def _detect_local_ipv4() -> str | None:
    # Discover the outbound local interface without sending traffic.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        entries = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        for entry in entries:
            ip = entry[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


def _extract_ipv4(text: str) -> str | None:
    candidates = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    for candidate in candidates:
        try:
            return str(ipaddress.IPv4Address(candidate))
        except ipaddress.AddressValueError:
            continue
    return None


def _is_public_ipv4(value: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _cashly_ctl_art(width: int, text: str, font: str) -> list[str]:
    if Figlet is None:
        return _center_art_block([text], width)

    selected_font = font.strip() or "slant"
    try:
        figlet = Figlet(font=selected_font, width=max(120, width))
    except Exception:
        figlet = Figlet(font="slant", width=max(120, width))

    raw = figlet.renderText(text).rstrip("\n")
    lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return _center_art_block([text], width)
    return _center_art_block(lines, width)


def _center_art_block(lines: list[str], width: int) -> list[str]:
    if not lines:
        return []
    longest = max(len(line) for line in lines)
    block_width = min(longest, width)
    left = max(0, (width - block_width) // 2)

    rendered: list[str] = []
    for line in lines:
        clipped = line[:block_width]
        rendered.append((" " * left) + clipped)
    return rendered
