from __future__ import annotations

import ipaddress
import hmac
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, cast

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
from cashlyctl.auth import (
    LocalAuthError,
    create_initial_admin_user,
    has_local_users,
    load_login_credentials,
    local_user_count,
    role_for_login_user,
    verify_local_login,
    verify_login,
)
from cashlyctl.commands import CommandKind, parse_command
from cashlyctl.config import (
    QUERIES_DIR,
    STATE_DIR,
    ensure_state_layout,
    load_config,
    save_config,
)
from cashlyctl.crm_auth import (
    CrmAuthError,
    autodialer_macro_spec,
    autodialer_macro_specs,
    attempt_open_pairing_url,
    default_device_label,
    load_crm_device_session,
    poll_crm_pairing,
    save_crm_device_session,
    send_autodialer_macro,
    start_crm_pairing,
    verify_crm_device_session,
)
from cashlyctl.deployments import (
    DeployReadinessResult,
    DeployRunResult,
    DeploySpec,
    DeployStepResult,
    fetch_deploy_preview_info,
    load_deploy_specs,
    probe_deploy_readiness,
    probe_deploy_target_readiness,
    run_deploy_via_ssh,
    run_rollback_via_ssh,
)
from cashlyctl.health import run_mvp_checks
from cashlyctl.host_inspect import HostInspection, inspect_host
from cashlyctl.hotkeys import autodialer_macro_hotkey, next_contact_hotkey
from cashlyctl.models import DeploymentMode, Environment, HealthCheckResult, Profile
from cashlyctl.network_probe import NetworkProbeResult, NetworkProbeTarget, probe_targets
from cashlyctl.runtime_env import runtime_env


class AppState(StrEnum):
    PREAUTH = "PREAUTH"
    OBSERVE = "OBSERVE"
    MAINT = "MAINT"
    SERVICE = "SERVICE"


class LogonFlowStage(StrEnum):
    NONE = "NONE"
    USER = "USER"
    PASSWORD = "PASSWORD"
    INIT_ADMIN_USER = "INIT_ADMIN_USER"
    INIT_ADMIN_PASSWORD = "INIT_ADMIN_PASSWORD"
    INIT_ADMIN_CONFIRM = "INIT_ADMIN_CONFIRM"


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
        self.local_user_count = local_user_count()
        self.logo_text = os.getenv("CASHLYCTL_ASCII_TEXT", "cashlyCTL")
        self.logo_font = os.getenv("CASHLYCTL_ASCII_FONT", "slant")
        self.host_inspection = inspect_host()
        self.host_ip_label, self.host_ip = _detect_banner_ip()
        self.selected_postauth_state = AppState.OBSERVE
        self.target_state_selected = False
        self.job_status = "IDLE"
        self.logon_stage = LogonFlowStage.NONE
        self.pending_logon_user = ""
        self.pending_init_admin_user = ""
        self.pending_init_admin_password = ""
        self.service_confirm_required = False
        self.service_confirm_scope = ""
        self.service_idle_ttl_seconds = _service_idle_ttl_seconds()
        self.service_last_activity_ts = time.monotonic()
        self.jobs_session_id = ""
        self.jobs_session_started_at = ""
        self.jobs_session_file: Path | None = None
        self.jobs_records: list[dict[str, str]] = []
        self.boot_checks_lines: list[str] = []
        self.boot_checks_sequence: list[
            tuple[str, str, Callable[[], tuple[str, int, str]] | None]
        ] = []
        self.boot_checks_index = 0
        self.boot_checks_running = False
        self.boot_checks_wait_for_enter = False
        self.boot_scroll_offset = 0
        self.boot_checks_timer = None
        self.boot_checks_seq = 0
        self.boot_check_active_index: int | None = None
        self.boot_check_active_label = ""
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
        self.deploy_readiness_silent_seq: dict[int, bool] = {}
        self.deploy_history: list[dict[str, str]] = []
        self.last_deploy_report: DeployRunResult | None = None
        self.deploy_state = "IDLE"
        self.deploy_preview_target = ""
        self.deploy_preview_revision = ""
        self.deploy_preview_tag = ""
        self.deploy_preview_branch = "-"
        self.deploy_preview_current_sha = "-"
        self.deploy_preview_target_sha = "-"
        self.deploy_preview_latest_pr = "-"
        self.deploy_preview_last_deploy_time = "-"
        self.deploy_preview_last_good = "-"
        self.deploy_preview_preflight_summary = "UNKNOWN"
        self.deploy_preview_required_confirm = ""
        self.deploy_preview_confirmation_hint = ""
        self.deploy_preview_info_error = ""
        self.deploy_job_id_seq = 184
        self.deploy_job_id = ""
        self.deploy_job_running = False
        self.deploy_job_target = ""
        self.deploy_job_phase_seen: set[int] = set()
        self.deploy_job_lines: list[str] = []
        self.deploy_job_result: DeployRunResult | None = None
        self.deploy_job_seq = 0
        self.aws_sso_profiles = self._configured_aws_sso_profiles()
        self.aws_sso_status: dict[str, dict[str, str]] = {
            profile: {
                "status": "UNKNOWN",
                "detail": "not checked",
                "checked_at": "-",
            }
            for profile in self.aws_sso_profiles
        }
        self.aws_sso_status_loading = False
        self.aws_sso_status_seq = 0
        self.aws_sso_login_running = False
        self.aws_sso_login_targets: list[str] = []
        self.crm_pair_running = False
        self.crm_pair_seq = 0
        self.crm_pair_lines: list[str] = []
        self.crm_pair_started_at = "-"
        self.crm_pair_result_status = "IDLE"
        self.crm_device_status = self._summarize_crm_device_session()
        self.crm_macro_running = False
        self.crm_macro_seq = 0
        self.crm_macro_lines: list[str] = []
        self.crm_macro_started_at = "-"
        self.crm_macro_result_status = "IDLE"

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
        self._start_aws_sso_status_refresh("PREAUTH")
        if self.local_user_count or self.login_credentials:
            self._set_status("ENTER LOGON TO START AUTH", "ok")
        else:
            self._set_status("NO LOCAL USERS. TYPE INITADMIN TO CREATE ADMIN", "warn")

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
        elif parsed.kind == CommandKind.INIT_ADMIN:
            audit_command(self.active_profile.name, self._redacted_init_admin_audit(parsed.value))
            self._record_session_job("COMMAND", self._redacted_init_admin_audit(parsed.value))
        else:
            audit_command(self.active_profile.name, raw)
            self._record_session_job("COMMAND", raw)
        self._execute(raw)

    def action_help_key(self) -> None:
        self._record_activity()
        self._set_job_status("SHOWING HELP...")
        self._set_status(
            "HELP: INITADMIN, LOGON, LOGOFF, SET STATE <observe|maint|service>, CRM PAIR, CRM STATUS, CRM START/NEXT/PAUSE/RESUME/STOP, SSO STATUS, SSO LOGIN <profile|ALL>, SERVICE ON, PROCEED <target>, =0..=8, EXIT, REFRESH, PROFILE, SET ENV, TAIL, DETAIL, DEPLOY (preview), CONFIRM DEPLOY ..., ROLLBACK, STATUS DEPLOY, DIFF, PLAN, SAVE QRY",
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
        sso_refresh_started = False
        if self.panel == "7":
            self._start_deploy_readiness_refresh("REFRESH")
            deploy_refresh_started = True
        if self.panel == "L":
            self._start_aws_sso_status_refresh("REFRESH")
            sso_refresh_started = True
        self.login_credentials = load_login_credentials()
        self.local_user_count = local_user_count()
        self.host_inspection = inspect_host()
        self.host_ip_label, self.host_ip = _detect_banner_ip()
        self._render()
        if deploy_refresh_started or sso_refresh_started:
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
            was_init_admin = self.logon_stage in {
                LogonFlowStage.INIT_ADMIN_USER,
                LogonFlowStage.INIT_ADMIN_PASSWORD,
                LogonFlowStage.INIT_ADMIN_CONFIRM,
            }
            self._end_logon_flow()
            self._set_job_status("AUTH CANCELED")
            cancel_status = "INITADMIN CANCELED" if was_init_admin else "LOGON CANCELED"
            self._set_status(cancel_status, "warn")
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
        if parsed.kind == CommandKind.INIT_ADMIN:
            self._handle_init_admin(parsed.value)
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
        if parsed.kind == CommandKind.AWS_SSO_STATUS:
            self._start_aws_sso_status_refresh("AWS SSO STATUS")
            return
        if parsed.kind == CommandKind.AWS_SSO_LOGIN:
            self._handle_aws_sso_login(parsed.value)
            return
        if parsed.kind == CommandKind.NUMBER and self.panel in {"L", "P"}:
            self._handle_number(parsed.value)
            return

        if not self.authenticated:
            self._set_status("LOGON REQUIRED: TYPE LOGON", "warn")
            return

        if parsed.kind == CommandKind.CRM_PAIR:
            self._handle_crm_pair(parsed.value)
            return
        if parsed.kind == CommandKind.CRM_STATUS:
            self._handle_crm_status()
            return
        if parsed.kind == CommandKind.CRM_NEXT:
            self._handle_crm_autodialer_macro("next-contact")
            return
        if parsed.kind == CommandKind.CRM_MACRO:
            self._handle_crm_autodialer_macro(parsed.value)
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
        if parsed.kind == CommandKind.CONFIRM_DEPLOY:
            self._handle_confirm_deploy(parsed.raw.strip())
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

    def _handle_init_admin(self, init_args: str) -> None:
        if has_local_users():
            self.local_user_count = local_user_count()
            self._set_status("LOCAL USERS ALREADY INITIALIZED. TYPE LOGON", "warn")
            self._set_job_status("INITADMIN BLOCKED")
            return
        if self.authenticated:
            self._set_status("ALREADY LOGGED ON", "warn")
            self._set_job_status("INITADMIN BLOCKED")
            return

        args = init_args.strip()
        if not args:
            self._start_init_admin_user_prompt()
            return

        parts = args.split()
        self._start_init_admin_password_prompt(parts[0])
        if len(parts) > 1:
            self._set_status(
                "INLINE PASSWORD IGNORED. ENTER PASSWORD IN MASKED PROMPT.",
                "warn",
            )

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
        if stage_command.kind == CommandKind.INIT_ADMIN and self.logon_stage not in {
            LogonFlowStage.INIT_ADMIN_PASSWORD,
            LogonFlowStage.INIT_ADMIN_CONFIRM,
        }:
            self._handle_init_admin(stage_command.value)
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

        if self.logon_stage == LogonFlowStage.INIT_ADMIN_USER:
            username = raw.strip().split()[0] if raw.strip() else ""
            if not username:
                self._set_status("ADMIN USERID CANNOT BE EMPTY", "error")
                self._set_job_status("INITADMIN FAILED")
                return
            self._start_init_admin_password_prompt(username)
            return

        if self.logon_stage == LogonFlowStage.INIT_ADMIN_PASSWORD:
            password = raw
            if len(password) < 8:
                self.pending_init_admin_password = ""
                self._set_status("PASSWORD MUST BE AT LEAST 8 CHARACTERS", "error")
                self._set_job_status("INITADMIN FAILED")
                self._start_init_admin_password_prompt(self.pending_init_admin_user)
                return
            self.pending_init_admin_password = password
            self.logon_stage = LogonFlowStage.INIT_ADMIN_CONFIRM
            self._set_command_mode(label="Again ===>", password=True, placeholder="confirm")
            self._set_job_status("INITADMIN: CONFIRM PASSWORD")
            self._set_status(f"CONFIRM PASSWORD FOR {self.pending_init_admin_user}", "ok")
            return

        if self.logon_stage == LogonFlowStage.INIT_ADMIN_CONFIRM:
            username = self.pending_init_admin_user
            password = self.pending_init_admin_password
            confirmation = raw
            if not username or not password:
                self._end_logon_flow()
                self._set_status("INITADMIN FLOW RESET. TYPE INITADMIN AGAIN.", "warn")
                self._set_job_status("INITADMIN IDLE")
                return
            if not hmac.compare_digest(password, confirmation):
                self.pending_init_admin_password = ""
                self._set_status("PASSWORDS DO NOT MATCH", "error")
                self._set_job_status("INITADMIN FAILED")
                self._start_init_admin_password_prompt(username)
                return
            self._end_logon_flow()
            self._create_initial_admin_and_logon(username, password)
            return

        self._end_logon_flow()
        audit_command(self.active_profile.name, raw)
        self._execute(raw)

    def _start_init_admin_user_prompt(self) -> None:
        self.logon_stage = LogonFlowStage.INIT_ADMIN_USER
        self.pending_init_admin_user = ""
        self.pending_init_admin_password = ""
        self._set_command_mode(label="Admin ===>", password=False, placeholder="userid")
        self._set_job_status("INITADMIN: USERID REQUIRED")
        self._set_status("ENTER LOCAL ADMIN USERID", "ok")

    def _start_init_admin_password_prompt(self, username: str) -> None:
        user = username.strip().lower()
        if not user:
            self._set_status("ADMIN USERID CANNOT BE EMPTY", "error")
            self._set_job_status("INITADMIN FAILED")
            return
        self.logon_stage = LogonFlowStage.INIT_ADMIN_PASSWORD
        self.pending_init_admin_user = user
        self.pending_init_admin_password = ""
        self._set_command_mode(label="Pass ===>", password=True, placeholder="min 8 chars")
        self._set_job_status("INITADMIN: PASSWORD REQUIRED")
        self._set_status(f"ENTER PASSWORD FOR LOCAL ADMIN {user}", "ok")

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
        self.pending_init_admin_user = ""
        self.pending_init_admin_password = ""
        self._set_command_mode()

    def _create_initial_admin_and_logon(self, username: str, password: str) -> None:
        self._set_job_status("CREATING LOCAL ADMIN...")
        try:
            created = create_initial_admin_user(username, password)
        except LocalAuthError as exc:
            self._set_status(f"INITADMIN FAILED: {exc}", "error")
            self._set_job_status("INITADMIN FAILED")
            return

        self.local_user_count = local_user_count()
        audit_command(self.active_profile.name, f"INITADMIN {created.username}")
        self._record_session_job("INITADMIN", f"local admin created: {created.username}", "OK")
        self._authenticate_logon(created.username, password)

    def _authenticate_logon(self, username: str, password: str) -> None:
        self._set_job_status("AUTHENTICATING USER...")
        user = username.strip()
        if not user or not password:
            self._set_status("USAGE: LOGON then USERID then PASSWORD", "error")
            self._set_job_status("AUTH FAILED")
            return

        local_auth = verify_local_login(user, password)
        role = ""
        if local_auth:
            user = local_auth.username
            role = local_auth.role
        else:
            self.login_credentials = load_login_credentials()
            if verify_login(user, password, self.login_credentials):
                role = self._role_for_user(user)

        if not role and not has_local_users() and not self.login_credentials:
            self._set_status("NO LOCAL USERS. TYPE INITADMIN TO CREATE ADMIN", "error")
            self._set_job_status("AUTH FAILED")
            return
        if not role:
            self._set_status("LOGON FAILED: INVALID CREDENTIALS", "error")
            self._set_job_status("AUTH FAILED")
            self.panel = "L"
            self._render()
            return

        if not self.target_state_selected:
            self.selected_postauth_state = AppState.OBSERVE

        requested_state = self.selected_postauth_state
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

        self.crm_pair_seq += 1
        self.crm_pair_running = False
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
        self.boot_check_active_index = None
        self.boot_check_active_label = ""
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
        self.deploy_readiness_silent_seq = {}
        self.deploy_state = "IDLE"
        self.deploy_preview_target = ""
        self.deploy_preview_latest_pr = "-"
        self.deploy_preview_required_confirm = ""
        self.deploy_preview_confirmation_hint = ""
        self.deploy_job_running = False
        self.deploy_job_lines = []
        self.deploy_job_result = None
        self.crm_pair_result_status = "IDLE"
        self.crm_pair_started_at = "-"
        self.crm_pair_lines = []
        self.crm_device_status = self._summarize_crm_device_session()

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
                if not self.deploy_readiness and not self.deploy_readiness_loading:
                    self._start_deploy_readiness_refresh("PANEL OPEN")
                    self._set_job_status("PANEL 7 LOADING PRECHECKS...", render_now=False)
                    self._set_status("PANEL 7 OPEN. DEPLOY PRECHECKS RUNNING...", "warn")
                    return
                if self.deploy_readiness_loading:
                    self._set_job_status("PANEL 7 LOADING PRECHECKS...", render_now=False)
                    self._set_status("PANEL 7 OPEN. USING PRELOAD CHECKS...", "warn")
                    return
                self._set_job_status("PANEL 7 READY", render_now=False)
                self._set_status("PANEL 7 OPEN. PREFLIGHT CACHE READY.", "ok")
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
        if self.panel == "8":
            if value == "1":
                self._goto_panel("8A")
                self._set_job_status("PANEL 8A READY")
                self._set_status("CASHLYCRM PAIRING", "ok")
                return
            if value == "2":
                self._goto_panel("8B")
                self._set_job_status("PANEL 8B READY")
                self._set_status("CASHLYCRM MACROS", "ok")
                return
            self._set_status("INVALID UTILITIES OPTION. USE 1 OR 2.", "warn")
            return
        if self.panel == "8A":
            if value == "1":
                self._handle_crm_pair("")
                return
            if value == "2":
                self._handle_crm_status()
                return
            self._set_status("INVALID PAIRING OPTION. USE 1 OR 2.", "warn")
            return
        if self.panel == "8B":
            macro_by_number = {
                str(index): spec.action
                for index, spec in enumerate(autodialer_macro_specs(), start=1)
            }
            if value in macro_by_number:
                self._handle_crm_autodialer_macro(macro_by_number[value])
                return
            if value == str(len(macro_by_number) + 1):
                self._handle_crm_status()
                return
            self._set_status(
                f"INVALID MACROS OPTION. USE 1-{len(macro_by_number) + 1}.",
                "warn",
            )
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
            self._open_deploy_preview(mapping[value])
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

    def _open_deploy_preview(
        self,
        target_value: str,
        revision: str = "",
        tag: str = "",
    ) -> None:
        target = self._normalize_deploy_target(target_value.strip().lower())
        if not target:
            self._set_status("UNKNOWN DEPLOY TARGET", "error")
            return

        spec = self._deploy_spec_for_target(target)
        readiness = probe_deploy_target_readiness(
            target,
            spec,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )
        self.deploy_readiness[target.lower()] = readiness
        self.deploy_readiness_last_refresh_at = readiness.checked_at

        branch = self._deploy_branch_from_ref(spec.default_ref if spec else "")
        preview = fetch_deploy_preview_info(
            spec,
            branch=branch,
            timeout_sec=self.deploy_readiness_timeout_seconds,
        )

        self.deploy_preview_target = target
        self.deploy_preview_revision = revision.strip()
        self.deploy_preview_tag = tag.strip()
        self.deploy_preview_branch = preview.branch
        self.deploy_preview_current_sha = preview.current_sha
        self.deploy_preview_target_sha = preview.target_sha
        self.deploy_preview_latest_pr = preview.latest_merged_pr
        self.deploy_preview_info_error = preview.error
        self.deploy_preview_last_deploy_time = (
            (self._latest_deploy_record(target) or {}).get("finished_at", "-")
        )
        self.deploy_preview_last_good = (spec.last_good_ref.strip() if spec else "") or "-"
        readiness_detail = self._deploy_readiness_summary(readiness)
        self.deploy_preview_preflight_summary = readiness_detail
        self.deploy_preview_required_confirm = self._build_deploy_confirm_phrase(
            target,
            preview.target_sha,
            preview.branch,
        )
        self.deploy_preview_confirmation_hint = (
            f"Type exactly: {self.deploy_preview_required_confirm}"
        )
        self.deploy_state = "PREVIEW"
        self.deploy_job_running = False
        self.deploy_job_result = None
        self.deploy_job_lines = []

        self._goto_panel("7A")
        self._set_job_status(f"DEPLOY PREVIEW READY: {self._deploy_target_code(target)}", render_now=False)
        self._set_status("CONFIRMATION REQUIRED. NOTHING EXECUTED YET.", "warn")
        self._render()

    def _build_deploy_confirm_phrase(
        self,
        target: str,
        target_sha: str,
        branch: str,
    ) -> str:
        code = self._deploy_target_code(target)
        if self._target_requires_service(target):
            stamp = datetime.now().strftime("%H:%M")
            return f"CONFIRM DEPLOY {code} IN SERVICE MODE AT {stamp}"

        revision_hint = self.deploy_preview_revision or self.deploy_preview_tag
        if not revision_hint:
            revision_hint = target_sha if target_sha != "-" else branch
        return f"CONFIRM DEPLOY {code} REV {revision_hint}"

    def _deploy_branch_from_ref(self, ref: str) -> str:
        value = (ref or "").strip()
        if not value:
            return "main"
        if value.startswith("origin/"):
            return value[7:] or "main"
        if value.startswith("refs/heads/"):
            return value[11:] or "main"
        return value

    def _deploy_target_code(self, target: str) -> str:
        normalized = target.strip().lower()
        mapping = {
            "cashly-app-development": "CRM-DEV",
            "cashly-app-production": "CRM-PROD",
            "n8n-server": "N8N",
        }
        return mapping.get(normalized, normalized.upper())

    def _handle_confirm_deploy(self, confirm_text: str) -> None:
        if self.panel != "7A" or self.deploy_state != "PREVIEW":
            self._set_status("CONFIRM DEPLOY IS ONLY VALID IN PANEL 7A PREVIEW", "warn")
            return
        expected = self.deploy_preview_required_confirm.strip()
        if not expected:
            self._set_status("NO DEPLOY PREVIEW CONFIRMATION PENDING", "warn")
            return
        provided = confirm_text.strip()
        if provided != expected:
            self._set_status("CONFIRMATION MISMATCH. TYPE THE EXACT STRING SHOWN IN PANEL 7A.", "error")
            return

        target = self.deploy_preview_target
        if not target:
            self._set_status("DEPLOY PREVIEW TARGET MISSING", "error")
            return
        if not self._mode_allows_write(target):
            return

        spec = self._deploy_spec_for_target(target)
        if not spec:
            self._set_job_status("DEPLOY BLOCKED")
            self._set_status(f"NO DEPLOY SSH CONFIG FOR {target}", "error")
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

        self.deploy_job_seq += 1
        seq = self.deploy_job_seq
        code = self._deploy_target_code(target).replace("-", "_")
        self.deploy_job_id = f"DEPLOY_{code} #{self.deploy_job_id_seq}"
        self.deploy_job_id_seq += 1
        self.deploy_job_target = target
        self.deploy_job_running = True
        self.deploy_state = "EXECUTING"
        self.deploy_job_phase_seen = set()
        self.deploy_job_result = None
        self.deploy_job_lines = [
            f"JOB: {self.deploy_job_id}",
            f"TARGET: {target}  BRANCH: {self.deploy_preview_branch}  REF: {self.deploy_preview_revision or self.deploy_preview_target_sha or self.deploy_preview_branch}",
            "",
        ]

        self._goto_panel("7B")
        self._set_job_status(f"DEPLOY EXECUTING: {self._deploy_target_code(target)}", render_now=False)
        self._set_status("DEPLOY JOB SUBMITTED", "warn")
        self._render()
        self.refresh()

        threading.Thread(
            target=self._deploy_job_worker,
            args=(
                seq,
                target,
                spec,
                self.deploy_preview_revision,
                self.deploy_preview_tag,
            ),
            daemon=True,
        ).start()

    def _deploy_job_worker(
        self,
        seq: int,
        target: str,
        spec: DeploySpec,
        revision: str,
        tag: str,
    ) -> None:
        def on_step(step: DeployStepResult) -> None:
            self.call_from_thread(self._append_deploy_job_step, seq, step)

        report = run_deploy_via_ssh(
            spec,
            revision=revision,
            tag=tag,
            on_step=on_step,
        )
        self.call_from_thread(self._finish_deploy_job, seq, target, report)

    def _append_deploy_job_step(self, seq: int, step: DeployStepResult) -> None:
        if seq != self.deploy_job_seq:
            return
        phase_idx, phase_label = self._deploy_phase(step.name)
        if phase_idx not in self.deploy_job_phase_seen:
            self.deploy_job_phase_seen.add(phase_idx)
            self.deploy_job_lines.append(
                f"STEP {phase_idx}/7 {phase_label}... {step.status}"
            )
        elif step.status in {"FAIL", "WARN"}:
            detail = step.detail.replace("\n", " ")
            self.deploy_job_lines.append(f"  {step.name}: {step.status} {detail[:140]}")
        self._render()
        self.refresh()

    def _finish_deploy_job(self, seq: int, target: str, report: DeployRunResult) -> None:
        if seq != self.deploy_job_seq:
            return
        self.deploy_job_running = False
        self.deploy_job_result = report
        self.last_deploy_report = report
        self._record_deploy_report(report)

        final_step_status = "OK" if report.status == "OK" else ("WARN" if report.status == "WARN" else "FAIL")
        self.deploy_job_lines.append(
            f"STEP 7/7 Mark last-known-good... {final_step_status}"
        )

        if report.status == "OK":
            self.deploy_state = "COMPLETE"
            self._set_job_status(f"DEPLOY COMPLETE: {self._deploy_target_code(target)}", render_now=False)
            self._set_status(f"DEPLOY COMPLETE: {target} ref={report.ref}", "ok")
        elif report.status == "WARN":
            self.deploy_state = "COMPLETE"
            self._set_job_status(f"DEPLOY COMPLETE WITH WARN: {self._deploy_target_code(target)}", render_now=False)
            self._set_status(f"DEPLOY COMPLETE WITH WARN: {target}", "warn")
        else:
            self.deploy_state = "FAILED"
            self._set_job_status(f"DEPLOY FAILED: {self._deploy_target_code(target)}", render_now=False)
            self._set_status(
                f"DEPLOY FAILED: {target}. CONSIDER ROLLBACK {target} --to last-good",
                "error",
            )

        self._render()
        self.refresh()

    def _deploy_phase(self, step_name: str) -> tuple[int, str]:
        name = step_name.upper()
        if name.startswith("PREFLIGHT"):
            return 1, "Validate mode + scope"
        if name in {"FETCH", "CHECKOUT"}:
            return 2, "Git fetch + checkout"
        if name == "NPM_CI":
            return 3, "npm ci"
        if name == "BUILD":
            return 4, "npm run build"
        if name.startswith("PM2") or name.startswith("NGINX"):
            return 5, "pm2 reload"
        if name.startswith("VERIFY"):
            return 6, "Health check (local + public)"
        return 6, step_name

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

        self._open_deploy_preview(target, revision=revision, tag=tag)

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

    def _handle_crm_pair(self, pair_args: str) -> None:
        if self.selected_postauth_state == AppState.OBSERVE:
            self._set_job_status("CRM PAIR BLOCKED")
            self._set_status("CRM PAIR REQUIRES MAINT MODE. USE SET STATE MAINT.", "error")
            return
        if self.crm_pair_running:
            self._set_status("CRM PAIR ALREADY RUNNING", "warn")
            return

        base_url = pair_args.strip() or None
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            self._set_status("USAGE: CRM PAIR [https://crm.example.com]", "warn")
            return

        self.crm_pair_seq += 1
        seq = self.crm_pair_seq
        self.crm_pair_running = True
        self.crm_pair_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.crm_pair_result_status = "RUNNING"
        self.crm_pair_lines = [
            "CRM PAIRING STARTED",
            f"LOCAL USER: {self.session_user}  ROLE: {self.session_role.upper()}  MODE: MAINT",
            "Starting browser-approved CashlyCRM device flow...",
        ]
        self._goto_panel("8A")
        self._set_job_status("CRM PAIR RUNNING", render_now=False)
        self._set_status("CRM PAIR STARTED. APPROVE IN BROWSER WHEN CODE APPEARS.", "warn")
        self._render()
        self.refresh()
        threading.Thread(
            target=self._crm_pair_worker,
            args=(seq, base_url),
            daemon=True,
        ).start()

    def _crm_pair_worker(self, seq: int, base_url: str | None) -> None:
        try:
            start = start_crm_pairing(base_url=base_url, device_label=default_device_label())
            self.call_from_thread(self._append_crm_pair_line, seq, f"OPEN URL: {start.verification_uri}")
            opened, open_detail = attempt_open_pairing_url(start.verification_uri)
            self.call_from_thread(
                self._append_crm_pair_line,
                seq,
                f"AUTO OPEN: {'YES' if opened else 'NO'} - {open_detail}",
            )
            self.call_from_thread(self._append_crm_pair_line, seq, f"USER CODE: {start.user_code}")
            self.call_from_thread(
                self._append_crm_pair_line,
                seq,
                f"EXPIRES IN: {start.expires_in}s  POLL: {start.interval}s",
            )

            deadline = time.monotonic() + start.expires_in
            next_notice = 0.0
            while time.monotonic() < deadline:
                if seq != self.crm_pair_seq:
                    return
                poll = poll_crm_pairing(start.base_url, start.device_code)
                if poll.status == "approved" and poll.token:
                    session = save_crm_device_session(
                        start.base_url,
                        poll.token,
                        poll.token_type,
                        poll.device,
                    )
                    device_id = session.device.get("id", "-")
                    org_id = session.device.get("organizationId", "-")
                    self.call_from_thread(
                        self._finish_crm_pair,
                        seq,
                        "OK",
                        f"PAIRED device={device_id} organization={org_id}",
                    )
                    return
                if poll.status not in {"pending", "authorization_pending"}:
                    message = poll.error or f"Pairing failed: {poll.status}"
                    self.call_from_thread(self._finish_crm_pair, seq, "FAIL", message)
                    return
                now = time.monotonic()
                if now >= next_notice:
                    self.call_from_thread(
                        self._append_crm_pair_line,
                        seq,
                        "Waiting for browser approval...",
                    )
                    next_notice = now + 15
                time.sleep(max(1, poll.interval or start.interval))
            self.call_from_thread(self._finish_crm_pair, seq, "FAIL", "Pairing timed out.")
        except CrmAuthError as exc:
            self.call_from_thread(self._finish_crm_pair, seq, "FAIL", str(exc))
        except Exception as exc:
            self.call_from_thread(self._finish_crm_pair, seq, "FAIL", str(exc))

    def _append_crm_pair_line(self, seq: int, line: str) -> None:
        if seq != self.crm_pair_seq:
            return
        self.crm_pair_lines.append(line)
        self.crm_pair_lines = self.crm_pair_lines[-30:]
        self._render()
        self.refresh()

    def _finish_crm_pair(self, seq: int, status: str, detail: str) -> None:
        if seq != self.crm_pair_seq:
            return
        self.crm_pair_running = False
        self.crm_pair_result_status = status
        self.crm_pair_lines.append(f"RESULT: {status}  {detail}")
        self.crm_device_status = self._summarize_crm_device_session()
        level = "ok" if status == "OK" else "error"
        self._set_job_status(f"CRM PAIR {status}", render_now=False)
        self._set_status(detail, level)
        self._record_session_job("CRM_PAIR", detail, status)
        self._render()
        self.refresh()

    def _handle_crm_status(self) -> None:
        if self.panel not in {"8A", "8B"}:
            self._goto_panel("8A")
        session = load_crm_device_session()
        if not session:
            self.crm_device_status = "NOT PAIRED"
            self._set_job_status("CRM STATUS READY", render_now=False)
            self._set_status("NO CASHLYCRM DEVICE SESSION STORED", "warn")
            self._render()
            return
        try:
            data = verify_crm_device_session(session)
        except CrmAuthError as exc:
            self.crm_device_status = f"INVALID: {exc}"
            self._set_job_status("CRM STATUS INVALID", render_now=False)
            self._set_status(f"CRM DEVICE INVALID: {exc}", "error")
            self._render()
            return

        device = data.get("device") if isinstance(data.get("device"), dict) else {}
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        organization = data.get("organization") if isinstance(data.get("organization"), dict) else {}
        self.crm_device_status = (
            f"PAIRED device={device.get('id', '-')} "
            f"profile={profile.get('email') or profile.get('fullName') or '-'} "
            f"org={organization.get('name') or organization.get('id') or '-'}"
        )
        self._set_job_status("CRM STATUS READY", render_now=False)
        self._set_status("CRM DEVICE SESSION VERIFIED", "ok")
        self._render()

    def _handle_crm_next_contact(self) -> None:
        self._handle_crm_autodialer_macro("next-contact")

    def _handle_crm_autodialer_macro(self, action: str) -> None:
        if self.selected_postauth_state == AppState.OBSERVE:
            self._set_job_status("CRM MACRO BLOCKED")
            self._set_status("CRM MACROS REQUIRE MAINT MODE. USE SET STATE MAINT.", "error")
            return
        if self.crm_macro_running:
            self._set_status("CRM MACRO ALREADY RUNNING", "warn")
            return
        try:
            spec = autodialer_macro_spec(action)
        except ValueError as exc:
            self._set_job_status("CRM MACRO INVALID")
            self._set_status(str(exc), "error")
            return

        self.crm_macro_seq += 1
        seq = self.crm_macro_seq
        self.crm_macro_running = True
        self.crm_macro_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.crm_macro_result_status = "RUNNING"
        self.crm_macro_lines = [
            "CRM MACRO STARTED",
            f"LOCAL USER: {self.session_user}  ROLE: {self.session_role.upper()}  MODE: MAINT",
            f"MACRO: {spec.label}",
            f"Queuing {spec.command_type} command...",
        ]
        self._goto_panel("8B")
        self._set_job_status("CRM MACRO RUNNING", render_now=False)
        self._set_status(f"{spec.command} QUEUEING", "warn")
        self._render()
        self.refresh()
        threading.Thread(
            target=self._crm_autodialer_macro_worker,
            args=(seq, spec.action),
            daemon=True,
        ).start()

    def _crm_autodialer_macro_worker(self, seq: int, action: str) -> None:
        try:
            command = send_autodialer_macro(action)
            self.call_from_thread(
                self._finish_crm_macro,
                seq,
                "OK",
                (
                    f"QUEUED command={command.command_id} "
                    f"type={command.command_type} status={command.status}"
                ),
            )
        except CrmAuthError as exc:
            self.call_from_thread(self._finish_crm_macro, seq, "FAIL", str(exc))
        except Exception as exc:
            self.call_from_thread(self._finish_crm_macro, seq, "FAIL", str(exc))

    def _finish_crm_macro(self, seq: int, status: str, detail: str) -> None:
        if seq != self.crm_macro_seq:
            return
        self.crm_macro_running = False
        self.crm_macro_result_status = status
        self.crm_macro_lines.append(f"RESULT: {status}  {detail}")
        level = "ok" if status == "OK" else "error"
        self._set_job_status(f"CRM MACRO {status}", render_now=False)
        self._set_status(detail, level)
        self._record_session_job("CRM_MACRO", detail, status)
        self._render()
        self.refresh()

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
        return role_for_login_user(username)

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
        self.deploy_readiness_silent_seq = {}
        self.deploy_state = "IDLE"
        self.deploy_preview_target = ""
        self.deploy_preview_latest_pr = "-"
        self.deploy_preview_required_confirm = ""
        self.deploy_preview_confirmation_hint = ""
        self.deploy_job_running = False
        self.deploy_job_lines = []
        self.deploy_job_result = None
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

    def _start_deploy_readiness_refresh(self, reason: str, silent: bool = False) -> None:
        if self.deploy_readiness_loading:
            return
        self.deploy_readiness_loading = True
        self.deploy_readiness_spinner_index = 0
        self.deploy_readiness_seq += 1
        seq = self.deploy_readiness_seq
        self.deploy_readiness_silent_seq[seq] = silent
        if not silent:
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
        silent = self.deploy_readiness_silent_seq.pop(seq, False)
        self.deploy_readiness_loading = False
        if error:
            if not silent:
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
        if not silent:
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

    def _configured_aws_sso_profiles(self) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def add_profile(raw: str) -> None:
            profile = raw.strip()
            if not profile:
                return
            key = profile.lower()
            if key in seen:
                return
            seen.add(key)
            ordered.append(profile)

        add_profile(_runtime_env("CASHLYCTL_AWS_PROFILE", ""))
        add_profile(_runtime_env("AWS_PROFILE", ""))
        for target in self.network_probe_targets:
            target_key = "".join(char if char.isalnum() else "_" for char in target.name).upper()
            add_profile(_runtime_env(f"CASHLYCTL_AWS_PROFILE_{target_key}", ""))
        return ordered

    def _refresh_aws_sso_profile_list(self) -> None:
        profiles = self._configured_aws_sso_profiles()
        self.aws_sso_profiles = profiles
        self.aws_sso_status = {
            profile: self.aws_sso_status.get(
                profile,
                {
                    "status": "UNKNOWN",
                    "detail": "not checked",
                    "checked_at": "-",
                },
            )
            for profile in profiles
        }

    def _start_aws_sso_status_refresh(self, reason: str) -> None:
        if self.aws_sso_status_loading:
            return
        self._refresh_aws_sso_profile_list()
        if not self.aws_sso_profiles:
            self._set_job_status("AWS SSO STATUS: NO PROFILES", render_now=False)
            self._set_status("NO AWS SSO PROFILES CONFIGURED", "warn")
            self._render()
            self.refresh()
            return

        self.aws_sso_status_loading = True
        self.deploy_readiness_spinner_index = 0
        self.aws_sso_status_seq += 1
        seq = self.aws_sso_status_seq
        self._set_job_status("AWS SSO STATUS CHECK RUNNING...", render_now=False)
        self._set_status(f"{reason}: CHECKING AWS SSO PROFILE STATUS...", "warn")
        self._render()
        self.refresh()
        threading.Thread(
            target=self._aws_sso_status_worker,
            args=(seq, list(self.aws_sso_profiles)),
            daemon=True,
        ).start()

    def _aws_sso_status_worker(self, seq: int, profiles: list[str]) -> None:
        try:
            results: dict[str, dict[str, str]] = {}
            for profile in profiles:
                status, detail = self._check_aws_sso_profile(profile)
                results[profile] = {
                    "status": status,
                    "detail": detail,
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            self.call_from_thread(self._finish_aws_sso_status_refresh, seq, results, "")
        except Exception as exc:
            self.call_from_thread(self._finish_aws_sso_status_refresh, seq, {}, str(exc))

    def _finish_aws_sso_status_refresh(
        self,
        seq: int,
        results: dict[str, dict[str, str]],
        error: str,
    ) -> None:
        if seq != self.aws_sso_status_seq:
            return
        self.aws_sso_status_loading = False
        if error:
            self._set_job_status("AWS SSO STATUS CHECK FAILED", render_now=False)
            self._set_status(f"AWS SSO STATUS CHECK FAILED: {error}", "error")
            self._render()
            self.refresh()
            return

        self.aws_sso_status = results
        ready = sum(1 for row in results.values() if row.get("status") == "READY")
        total = len(results)
        if total == 0:
            self._set_job_status("AWS SSO STATUS: NO PROFILES", render_now=False)
            self._set_status("NO AWS SSO PROFILES CONFIGURED", "warn")
        elif ready == total:
            self._set_job_status("AWS SSO READY", render_now=False)
            self._set_status(f"AWS SSO READY: {ready}/{total} profiles", "ok")
        else:
            self._set_job_status("AWS SSO PARTIAL", render_now=False)
            self._set_status(f"AWS SSO PARTIAL: {ready}/{total} profiles ready", "warn")
        self._render()
        self.refresh()

    def _handle_aws_sso_login(self, value: str) -> None:
        self._refresh_aws_sso_profile_list()
        raw = value.strip()
        if self.aws_sso_login_running:
            self._set_status("AWS SSO LOGIN ALREADY RUNNING", "warn")
            return
        if not raw:
            self._set_status("USAGE: SSO LOGIN <profile|ALL>", "warn")
            return
        if raw.upper() == "ALL":
            profiles = list(self.aws_sso_profiles)
        else:
            profiles = [raw]
        if not profiles:
            self._set_status("NO AWS SSO PROFILES CONFIGURED", "warn")
            return

        self.aws_sso_login_running = True
        self.aws_sso_login_targets = profiles
        self.deploy_readiness_spinner_index = 0
        joined = ", ".join(profiles)
        self._set_job_status("AWS SSO LOGIN RUNNING", render_now=False)
        self._set_status(f"AWS SSO LOGIN STARTED: {joined}", "warn")
        self._render()
        self.refresh()
        threading.Thread(
            target=self._aws_sso_login_worker,
            args=(profiles,),
            daemon=True,
        ).start()

    def _aws_sso_login_worker(self, profiles: list[str]) -> None:
        results: list[tuple[str, bool, str]] = []
        for profile in profiles:
            ok, detail = self._run_aws_cli(
                ["sso", "login", "--profile", profile],
                timeout_sec=900,
            )
            results.append((profile, ok, detail))
        self.call_from_thread(self._finish_aws_sso_login, results)

    def _finish_aws_sso_login(self, results: list[tuple[str, bool, str]]) -> None:
        self.aws_sso_login_running = False
        self.aws_sso_login_targets = []
        failed = [profile for profile, ok, _ in results if not ok]
        if failed:
            self._set_job_status("AWS SSO LOGIN FAILED", render_now=False)
            self._set_status(f"AWS SSO LOGIN FAILED: {', '.join(failed)}", "error")
        else:
            self._set_job_status("AWS SSO LOGIN COMPLETE", render_now=False)
            self._set_status("AWS SSO LOGIN COMPLETE", "ok")
        self._start_aws_sso_status_refresh("AWS SSO LOGIN")

    def _check_aws_sso_profile(self, profile: str) -> tuple[str, str]:
        ok, output = self._run_aws_cli(
            ["sts", "get-caller-identity", "--profile", profile, "--output", "json"],
            timeout_sec=25,
        )
        if ok:
            try:
                payload = json.loads(output)
                account = str(payload.get("Account", "-"))
                return "READY", f"account={account}"
            except json.JSONDecodeError:
                return "READY", "identity ok"

        lower = output.lower()
        if "aws cli not found" in lower:
            return "CLI_MISSING", "aws cli not found in PATH"
        if "profile" in lower and "not found" in lower:
            return "MISSING", "profile not configured"
        if "sso session" in lower or "token has expired" in lower or "unable to locate credentials" in lower:
            return "EXPIRED", "run SSO LOGIN <profile>"
        if "accessdenied" in lower or "not authorized" in lower:
            return "DENIED", "access denied for role/account"
        first = output.splitlines()[0].strip() if output else "unknown error"
        return "ERROR", first[:120]

    @staticmethod
    def _run_aws_cli(args: list[str], timeout_sec: int = 30) -> tuple[bool, str]:
        cmd = ["aws", *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(5, timeout_sec),
                check=False,
            )
        except FileNotFoundError:
            return False, "aws cli not found in PATH"
        except subprocess.TimeoutExpired:
            return False, "aws cli command timed out"
        except Exception as exc:
            return False, f"aws cli failed: {exc}"

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode == 0:
            return True, stdout or "ok"
        detail = "\n".join(part for part in (stdout, stderr) if part).strip()
        return False, detail or f"exit={result.returncode}"

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
        boot_spinner_active = (
            self.boot_checks_running
            and self.panel == "B"
            and self.boot_check_active_index is not None
            and bool(self.boot_check_active_label)
        )
        if not (
            self.deploy_readiness_loading
            or self.aws_sso_status_loading
            or self.aws_sso_login_running
            or self.crm_pair_running
            or boot_spinner_active
        ):
            return
        self.deploy_readiness_spinner_index = (
            self.deploy_readiness_spinner_index + 1
        ) % len(self.deploy_readiness_spinner_frames)
        if boot_spinner_active:
            idx = self.boot_check_active_index
            if idx is not None and 0 <= idx < len(self.boot_checks_lines):
                frame = self.deploy_readiness_spinner_frames[
                    self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
                ]
                self.boot_checks_lines[idx] = self._boot_check_pending_line(
                    self.boot_check_active_label,
                    frame,
                )
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
        right_host_os = self._header_right_host_os()
        right_status = right_ttl or right_host_os
        right_env = f"ENV: {self._env_banner()}"
        right_col_width = max(len(right_env), len(right_time), len(right_status))
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
                right_status.rjust(right_col_width) if right_status else "",
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
        elif self.panel == "8":
            lines.extend(self._panel8_utilities_lines())
        elif self.panel == "8A":
            lines.extend(self._panel8_pairing_lines())
        elif self.panel == "8B":
            lines.extend(self._panel8_macros_lines())
        elif self.panel == "7A":
            lines.extend(self._panel7a_deploy_preview_lines())
        elif self.panel == "7B":
            lines.extend(self._panel7b_deploy_job_lines())
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
        right_host_os = self._header_right_host_os()
        right_status = right_ttl or right_host_os
        right_ip = f"{self.host_ip_label} = {self.host_ip}"
        right_col_width = max(len(right_ip), len(right_time), len(right_status))
        accent_style = self._accent_style()
        logo_style = self._mode_style()
        styled_logo_lines = [self._style_text(line, logo_style) for line in logo_lines]
        header_mode = self.selected_postauth_state if self.target_state_selected else AppState.PREAUTH
        signon_lines = [
            '===> Enter "LOGON" to start sign-on sequence',
            "===> Step 1: enter USERID",
            "===> Step 2: enter PASSWORD (masked)",
            "===> Inline password input is ignored for safety",
        ]
        if not self.local_user_count and not self.login_credentials:
            signon_lines = [
                '===> Enter "INITADMIN" to create the first local admin',
                "===> Step 1: enter admin USERID",
                "===> Step 2: enter PASSWORD twice (masked)",
                "===> Local passwords are stored as salted hashes",
            ]
        lines = [
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
                right_status.rjust(right_col_width) if right_status else "",
                left_style=accent_style,
                right_style=accent_style,
            ),
            "",
            "",
            self._center("Welcome to CashlyCTL Application Control System v0.1"),
            "",
            *styled_logo_lines,
            "",
            self._center("System Customization - Cashly Tech Services Inc.*"),
            "",
            *signon_lines,
            "",
            "===> Select target postauth state before logon:",
            "     1 = OBSERVE",
            "     2 = MAINT",
            "     3 = SERVICE (superadmin only)",
            "===> Command: SET STATE <observe|maint|service>",
            "===> Runtime service arm: SERVICE ON, then PROCEED <target>",
            "===> Role policy: admin=OBSERVE/MAINT, superadmin=ALL",
            "",
        ]
        lines.extend(self._preauth_sso_lines())
        lines.extend(
            [
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
        )
        return lines

    def _preauth_sso_lines(self) -> list[str]:
        self._refresh_aws_sso_profile_list()
        lines = ["===> AWS SSO profile readiness:"]
        if self.aws_sso_status_loading:
            frame = self.deploy_readiness_spinner_frames[
                self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
            ]
            lines.append(
                self._style_text(
                    f"     checking profiles [{frame}]...",
                    "bold #ffd700",
                )
            )
        if self.aws_sso_login_running and self.aws_sso_login_targets:
            frame = self.deploy_readiness_spinner_frames[
                self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
            ]
            targets = ", ".join(self.aws_sso_login_targets)
            lines.append(
                self._style_text(
                    f"     login running [{frame}] targets={targets}",
                    "bold #ffd700",
                )
            )

        if not self.aws_sso_profiles:
            lines.extend(
                [
                    "     no profiles configured (set CASHLYCTL_AWS_PROFILE_<TARGET>)",
                    "===> Commands: SSO STATUS | SSO LOGIN <profile|ALL>",
                    "",
                ]
            )
            return lines

        detail_width = max(20, self._width() - 44)
        for profile in self.aws_sso_profiles:
            row = self.aws_sso_status.get(
                profile,
                {
                    "status": "UNKNOWN",
                    "detail": "not checked",
                    "checked_at": "-",
                },
            )
            status_token = self._sso_status_token(row.get("status", "UNKNOWN"))
            detail = self._markup_safe(self._clip_cell(row.get("detail", "-"), detail_width))
            lines.append(f"     {profile:<20} {status_token} {detail}")

        lines.extend(
            [
                "===> Commands: SSO STATUS | SSO LOGIN <profile|ALL>",
                "",
            ]
        )
        return lines

    def _sso_status_token(self, status: str) -> str:
        token = (status or "UNKNOWN").strip().upper()
        style = self._sso_status_style(token)
        return self._style_text(f"{token:<11}", style)

    @staticmethod
    def _sso_status_style(status: str) -> str:
        value = status.strip().upper()
        if value == "READY":
            return "bold #00ff00"
        if value in {"EXPIRED", "UNKNOWN"}:
            return "bold #ffd700"
        if value in {"ERROR", "DENIED", "MISSING", "CLI_MISSING"}:
            return "bold #ff3030"
        return "white"

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
            "Live system checks only.",
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
        self.boot_checks_sequence = self._build_boot_check_operations()
        self.boot_checks_running = True
        self.boot_checks_wait_for_enter = False
        self.boot_scroll_offset = 0
        self.boot_check_active_index = None
        self.boot_check_active_label = ""
        self.panel = "B"
        self._set_job_status("POST-LOGON CHECKS RUNNING", render_now=False)
        self._set_status("RUNNING POST-LOGON SYSTEM CHECKS...", "ok")
        self._render()
        self.refresh()
        self.boot_checks_seq += 1
        seq = self.boot_checks_seq
        threading.Thread(
            target=self._boot_checks_worker,
            args=(seq,),
            daemon=True,
        ).start()

    def _boot_content_lines(self) -> list[str]:
        content = list(self.boot_checks_lines)
        if self.boot_checks_wait_for_enter:
            content.extend(["", "SYSTEM CHECKS COMPLETE - PRESS ENTER TO CONTINUE", ""])
        return content

    def _boot_viewport_size(self) -> int:
        # Keep room for global header/footer and panel caption lines.
        return max(8, self.size.height - 18)

    def _stop_boot_checks(self) -> None:
        self.boot_checks_running = False
        self.boot_check_active_index = None
        self.boot_check_active_label = ""
        self.boot_checks_seq += 1

    def _boot_checks_worker(self, seq: int) -> None:
        for kind, label, runner in self.boot_checks_sequence:
            if not self._boot_run_active(seq):
                return
            if kind == "phase":
                self.call_from_thread(self._boot_append_phase_line, seq, label)
                continue
            line_index = self.call_from_thread(self._boot_append_pending_check_line, seq, label)
            if line_index is None:
                return
            status, elapsed_ms, final_label = self._run_boot_check_runner(runner, label)
            if not self._boot_run_active(seq):
                return
            self.call_from_thread(
                self._boot_complete_check_line,
                seq,
                line_index,
                final_label,
                status,
                elapsed_ms,
            )
        if self._boot_run_active(seq):
            self.call_from_thread(self._finish_boot_checks, seq)

    def _boot_run_active(self, seq: int) -> bool:
        return self.boot_checks_running and seq == self.boot_checks_seq and self.panel == "B"

    def _boot_append_phase_line(self, seq: int, label: str) -> None:
        if not self._boot_run_active(seq):
            return
        self.boot_checks_lines.append(self._style_text(label, self._accent_style()))
        self._render()
        self.refresh()

    def _boot_append_pending_check_line(self, seq: int, label: str) -> int | None:
        if not self._boot_run_active(seq):
            return None
        frame = self.deploy_readiness_spinner_frames[
            self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
        ]
        self.boot_checks_lines.append(self._boot_check_pending_line(label, frame))
        index = len(self.boot_checks_lines) - 1
        self.boot_check_active_index = index
        self.boot_check_active_label = label
        self._render()
        self.refresh()
        return index

    def _boot_complete_check_line(
        self,
        seq: int,
        index: int,
        label: str,
        status: str,
        elapsed_ms: int,
    ) -> None:
        if not self._boot_run_active(seq):
            return
        if index < 0 or index >= len(self.boot_checks_lines):
            return
        if self.boot_check_active_index == index:
            self.boot_check_active_index = None
            self.boot_check_active_label = ""
        self.boot_checks_lines[index] = self._boot_check_line(
            f"{label} ({max(0, int(elapsed_ms))}ms)",
            status,
        )
        self._render()
        self.refresh()

    @staticmethod
    def _run_boot_check_runner(
        runner: Callable[[], tuple[str, int, str]] | None,
        fallback_label: str,
    ) -> tuple[str, int, str]:
        started = time.perf_counter()
        if runner is None:
            elapsed = int((time.perf_counter() - started) * 1000)
            return "WARN", max(0, elapsed), fallback_label
        try:
            status, elapsed_ms, label = runner()
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return "FAIL", max(0, elapsed), f"{fallback_label} ({exc})"
        normalized = status.strip().upper()
        if normalized not in {"OK", "WARN", "FAIL"}:
            normalized = "WARN"
        return normalized, max(0, int(elapsed_ms)), label

    def _finish_boot_checks(self, seq: int | None = None) -> None:
        if seq is not None and seq != self.boot_checks_seq:
            return
        self.boot_checks_running = False
        self.boot_check_active_index = None
        self.boot_check_active_label = ""
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
        }.get(status, self._accent_style())
        return self._line_lr(message, f"[ {status} ]", right_style=style)

    def _boot_check_pending_line(self, message: str, spinner: str) -> str:
        return self._line_lr(f"{message} [{spinner}]", "")

    def _build_boot_check_operations(
        self,
    ) -> list[tuple[str, str, Callable[[], tuple[str, int, str]] | None]]:
        sequence: list[tuple[str, str, Callable[[], tuple[str, int, str]] | None]] = []
        state: dict[str, object] = {
            "health_ready": False,
            "health_ms": 0,
            "network_ready": False,
            "network_ms": 0,
            "network_error": "",
            "deploy_specs_ready": False,
            "deploy_specs_ms": 0,
            "sso_profiles_ready": False,
            "sso_profiles_ms": 0,
        }

        def timed(operation: Callable[[], object]) -> tuple[object, int]:
            started = time.perf_counter()
            value = operation()
            elapsed = int((time.perf_counter() - started) * 1000)
            return value, max(0, elapsed)

        def ensure_health() -> int:
            if not cast(bool, state["health_ready"]):
                snapshot, elapsed = timed(lambda: run_mvp_checks(self.active_profile))
                self.health_results = cast(list[HealthCheckResult], snapshot)
                state["health_ready"] = True
                state["health_ms"] = elapsed
            return cast(int, state["health_ms"])

        def ensure_network() -> tuple[str, int]:
            if not cast(bool, state["network_ready"]):
                started = time.perf_counter()
                try:
                    if self.network_probe_targets:
                        self.network_probe_results = probe_targets(
                            self.network_probe_targets,
                            timeout_seconds=self.network_probe_timeout_seconds,
                            latency_warn_ms=self.network_probe_latency_warn_ms,
                            tls_warn_days=self.network_probe_tls_warn_days,
                        )
                        self.network_probe_last_refresh_monotonic = time.monotonic()
                        self.network_probe_last_refresh_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        self.network_probe_results = []
                    state["network_error"] = ""
                except Exception as exc:
                    state["network_error"] = str(exc)
                finally:
                    state["network_ready"] = True
                    state["network_ms"] = max(0, int((time.perf_counter() - started) * 1000))
            return cast(str, state["network_error"]), cast(int, state["network_ms"])

        def ensure_deploy_specs() -> int:
            if not cast(bool, state["deploy_specs_ready"]):
                specs, elapsed = timed(
                    lambda: load_deploy_specs([target.name for target in self.network_probe_targets])
                )
                self.deploy_specs = cast(dict[str, DeploySpec], specs)
                state["deploy_specs_ready"] = True
                state["deploy_specs_ms"] = elapsed
            return cast(int, state["deploy_specs_ms"])

        def ensure_sso_profiles() -> int:
            if not cast(bool, state["sso_profiles_ready"]):
                _, elapsed = timed(self._refresh_aws_sso_profile_list)
                state["sso_profiles_ready"] = True
                state["sso_profiles_ms"] = elapsed
            return cast(int, state["sso_profiles_ms"])

        def phase(title: str) -> None:
            sequence.append(("phase", f"-- {title} --", None))

        def check(label: str, runner: Callable[[], tuple[str, int, str]]) -> None:
            sequence.append(("check", label, runner))

        def immediate(label: str, status: str) -> Callable[[], tuple[str, int, str]]:
            return lambda: (status, 0, label)

        phase("Identity & Mode")
        check(
            f"Session authenticated as {self.session_user} ({self.session_role})",
            immediate(
                f"Session authenticated as {self.session_user} ({self.session_role})",
                "OK" if self.authenticated else "FAIL",
            ),
        )
        check(
            f"Role policy + mode loaded: role={self.session_role} mode={self.selected_postauth_state.value}",
            immediate(
                f"Role policy + mode loaded: role={self.session_role} mode={self.selected_postauth_state.value}",
                "OK",
            ),
        )
        check(
            f"Active profile resolved: {self.active_profile.name} ({self.active_profile.env.value}/{self.active_profile.mode.value})",
            immediate(
                f"Active profile resolved: {self.active_profile.name} ({self.active_profile.env.value}/{self.active_profile.mode.value})",
                "OK",
            ),
        )
        credential_summary = (
            f"Local auth loaded ({self.local_user_count} local users, "
            f"{len(self.login_credentials)} env fallback users)"
        )
        check(
            credential_summary,
            immediate(
                credential_summary,
                "OK" if self.local_user_count or self.login_credentials else "FAIL",
            ),
        )
        check(
            f"Session job log initialized ({self._jobs_log_display_path()})",
            immediate(
                f"Session job log initialized ({self._jobs_log_display_path()})",
                "OK" if self.jobs_session_file else "WARN",
            ),
        )

        phase("Control Plane")
        if self.active_profile.mode == DeploymentMode.ENTERPRISE:
            control_label = f"Control API reachability ({self.active_profile.control_api_base_url})"

            def control_runner() -> tuple[str, int, str]:
                health_ms = ensure_health()
                status = self._health_status_for_check("control_api")
                latency = self._health_latency_for_check("control_api")
                return status, latency if latency > 0 else health_ms, control_label

            check(control_label, control_runner)
        else:
            check(
                "Internal mode selected (direct service checks enabled)",
                immediate("Internal mode selected (direct service checks enabled)", "OK"),
            )

        phase("Network & DNS")
        check(
            f"Probe targets configured ({len(self.network_probe_targets)})",
            immediate(
                f"Probe targets configured ({len(self.network_probe_targets)})",
                "OK" if self.network_probe_targets else "WARN",
            ),
        )

        def dns_runner() -> tuple[str, int, str]:
            error, elapsed = ensure_network()
            if error:
                return "FAIL", elapsed, f"DNS resolution checks failed: {error}"
            if not self.network_probe_results:
                return "WARN", elapsed, "DNS/TLS checks not available (no targets/probe results)"
            states: list[str] = []
            max_dns_ms = 0
            for item in self.network_probe_results:
                max_dns_ms = max(max_dns_ms, item.dns_ms)
                if not item.dns_ok:
                    states.append("FAIL")
                elif item.dns_ms > self.network_probe_latency_warn_ms:
                    states.append("WARN")
                else:
                    states.append("OK")
            return (
                self._rollup_status(states),
                max_dns_ms,
                f"DNS resolution checks completed ({len(self.network_probe_results)} targets)",
            )

        def tls_runner() -> tuple[str, int, str]:
            error, elapsed = ensure_network()
            if error:
                return "FAIL", elapsed, f"TLS checks failed: {error}"
            if not self.network_probe_results:
                return "WARN", elapsed, "DNS/TLS checks not available (no targets/probe results)"
            states: list[str] = []
            max_tls_ms = 0
            for item in self.network_probe_results:
                max_tls_ms = max(max_tls_ms, item.tls_ms)
                if not item.tls_ok:
                    states.append("FAIL")
                elif item.tls_days_left < self.network_probe_tls_warn_days:
                    states.append("WARN")
                else:
                    states.append("OK")
            return (
                self._rollup_status(states),
                max_tls_ms,
                f"TLS chain/expiry checks completed ({len(self.network_probe_results)} targets)",
            )

        check("DNS resolution checks completed", dns_runner)
        check("TLS chain/expiry checks completed", tls_runner)

        phase("Core Services")
        if self.active_profile.mode == DeploymentMode.INTERNAL:
            if self.active_profile.neo4j_bolt_uri:
                neo4j_label = "Neo4j service reachability"

                def neo4j_runner() -> tuple[str, int, str]:
                    ensure_health()
                    status = self._health_status_for_check("neo4j")
                    latency = self._health_latency_for_check("neo4j")
                    return status, latency, neo4j_label

                check(neo4j_label, neo4j_runner)

                graph_label = "Graph read smoke query"

                def graph_runner() -> tuple[str, int, str]:
                    ensure_health()
                    neo4j_status = self._health_status_for_check("neo4j")
                    latency = self._health_latency_for_check("neo4j")
                    return (
                        neo4j_status if neo4j_status != "FAIL" else "FAIL",
                        latency,
                        graph_label,
                    )

                check(graph_label, graph_runner)

            if self.active_profile.dealsense_url:
                dealsense_label = "DealSense API health endpoint"

                def dealsense_runner() -> tuple[str, int, str]:
                    ensure_health()
                    status = self._health_status_for_check("dealsense")
                    latency = self._health_latency_for_check("dealsense")
                    return status, latency, dealsense_label

                check(dealsense_label, dealsense_runner)

            if not self.active_profile.neo4j_bolt_uri and not self.active_profile.dealsense_url:
                check(
                    "No internal core services configured for this profile",
                    immediate("No internal core services configured for this profile", "OK"),
                )
        else:
            check(
                "Service health delegated to enterprise Control API",
                immediate("Service health delegated to enterprise Control API", "OK"),
            )

        phase("Observability")

        def metrics_runner() -> tuple[str, int, str]:
            _, elapsed = ensure_network()
            status = "OK" if self.network_probe_results else "WARN"
            return status, elapsed, "Public metrics/edge telemetry availability"

        check("Public metrics/edge telemetry availability", metrics_runner)
        check(
            "Session log sink writable",
            immediate(
                "Session log sink writable",
                "OK" if self.jobs_session_file else "WARN",
            ),
        )

        phase("Deployment Integrity")

        def deploy_specs_runner() -> tuple[str, int, str]:
            elapsed = ensure_deploy_specs()
            total = len(self.deploy_specs)
            status = "OK" if total > 0 else "WARN"
            return status, elapsed, f"Deploy target specs loaded ({total})"

        def deploy_keys_runner() -> tuple[str, int, str]:
            ensure_deploy_specs()
            started = time.perf_counter()
            states: list[str] = []
            for spec in self.deploy_specs.values():
                if not spec.ssh_key_path:
                    states.append("WARN")
                elif Path(spec.ssh_key_path).exists():
                    states.append("OK")
                else:
                    states.append("FAIL")
            elapsed = max(0, int((time.perf_counter() - started) * 1000))
            status = self._rollup_status(states) if states else "WARN"
            return status, elapsed, "Deploy SSH key paths validated"

        def deploy_preflight_runner() -> tuple[str, int, str]:
            started = time.perf_counter()
            try:
                specs, readiness, checked_at = self._collect_deploy_readiness()
            except Exception as exc:
                elapsed = max(0, int((time.perf_counter() - started) * 1000))
                return "FAIL", elapsed, f"Deploy preflight cache warmup failed: {exc}"
            self.deploy_specs = specs
            self.deploy_readiness = readiness
            self.deploy_readiness_last_refresh_at = checked_at
            states = [item.status for item in readiness.values()]
            status = self._rollup_status(states) if states else "WARN"
            elapsed = max(0, int((time.perf_counter() - started) * 1000))
            return status, elapsed, f"Deploy preflight cache ready ({len(readiness)} targets)"

        check("Deploy target specs loaded", deploy_specs_runner)
        check("Deploy SSH key paths validated", deploy_keys_runner)
        check("Deploy preflight cache warmup", deploy_preflight_runner)

        phase("Safety Gates")
        if self.selected_postauth_state == AppState.OBSERVE:
            check(
                "Mode gate: OBSERVE (write locked)",
                immediate("Mode gate: OBSERVE (write locked)", "OK"),
            )
        elif self.selected_postauth_state == AppState.MAINT:
            check(
                "Mode gate: MAINT (controlled writes enabled)",
                immediate("Mode gate: MAINT (controlled writes enabled)", "OK"),
            )
        else:
            check(
                "Mode gate: SERVICE (write window enabled)",
                immediate("Mode gate: SERVICE (write window enabled)", "OK"),
            )
        check(
            "SERVICE arming guard",
            immediate("SERVICE arming guard", "OK" if not self.service_confirm_required else "WARN"),
        )
        check(
            "Role constraint policy loaded",
            immediate(
                "Role constraint policy loaded",
                "OK" if self.session_role in {"viewer", "admin", "superadmin"} else "WARN",
            ),
        )

        phase("Infrastructure (profile-dependent)")
        check(
            "AWS SDK availability",
            immediate("AWS SDK availability", "OK" if self.aws_sdk_ready else "FAIL"),
        )

        def sso_bindings_runner() -> tuple[str, int, str]:
            elapsed = ensure_sso_profiles()
            if not self.aws_sso_profiles:
                return "WARN", elapsed, "AWS SSO profile bindings"
            return "OK", elapsed, f"AWS SSO profile bindings ({len(self.aws_sso_profiles)})"

        check("AWS SSO profile bindings", sso_bindings_runner)

        for profile in self._configured_aws_sso_profiles():

            def profile_runner(profile_name: str = profile) -> tuple[str, int, str]:
                ensure_sso_profiles()
                started = time.perf_counter()
                sso_status, detail = self._check_aws_sso_profile(profile_name)
                elapsed = max(0, int((time.perf_counter() - started) * 1000))
                mapped = self._boot_status_from_sso(sso_status)
                self.aws_sso_status[profile_name] = {
                    "status": sso_status,
                    "detail": detail,
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                return mapped, elapsed, f"AWS SSO profile {profile_name} ({detail})"

            check(f"AWS SSO profile {profile}", profile_runner)

        return sequence

    def _health_status_for_check(self, name: str) -> str:
        lookup = name.strip().lower()
        for item in self.health_results:
            if item.name.strip().lower() == lookup:
                value = item.status.value.upper()
                if value in {"OK", "WARN", "FAIL"}:
                    return value
                return "WARN"
        return "WARN"

    def _health_latency_for_check(self, name: str) -> int:
        lookup = name.strip().lower()
        for item in self.health_results:
            if item.name.strip().lower() == lookup:
                return max(0, int(item.latency_ms))
        return 0

    @staticmethod
    def _rollup_status(states: list[str]) -> str:
        normalized = [item.strip().upper() for item in states if item]
        if not normalized:
            return "WARN"
        if "FAIL" in normalized:
            return "FAIL"
        if "WARN" in normalized:
            return "WARN"
        return "OK"

    @staticmethod
    def _boot_status_from_sso(status: str) -> str:
        value = status.strip().upper()
        if value == "READY":
            return "OK"
        if value in {"EXPIRED", "UNKNOWN"}:
            return "WARN"
        return "FAIL"

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
                f"  Region:       {detail.region}   AWS Profile: {detail.aws_profile}",
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
                "  1  Initialize Deployment CRM Dev",
                "  2  Initialize Deployment CRM Prod",
                "  3  Initialize Deployment n8n",
                "  4  Rollback",
                "  5  Deploy History",
                "  6  Release Management (later)",
                "",
                "COMMANDS:",
                "  DEPLOY crm-dev                 (opens 7A preview, does not execute)",
                "  DEPLOY crm-prod REV <sha>      (opens 7A preview with revision)",
                "  DEPLOY crm-prod TAG <tag>      (opens 7A preview with tag)",
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

    def _panel7a_deploy_preview_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        target = self.deploy_preview_target or "-"
        target_code = self._deploy_target_code(target) if target != "-" else "-"
        requested_ref = self._deploy_preview_requested_ref()
        readiness = self.deploy_readiness.get(target.lower()) if target != "-" else None
        preflight_status = readiness.status if readiness else "UNKNOWN"

        lines = [
            self._line_lr(f"PANEL: 7A  DEPLOY {target_code} (PREVIEW)", f"MODE: {state_mode}"),
            self._rule("="),
            "",
            f"DEPLOY STATE: {self.deploy_state}",
            f"Current deployed SHA: {self.deploy_preview_current_sha}",
            f"Latest merged PR: {self.deploy_preview_latest_pr}",
            f"Target SHA (default latest): {self.deploy_preview_target_sha}",
            f"Requested ref: {requested_ref}",
            f"Branch: {self.deploy_preview_branch}",
            f"Last deploy time: {self.deploy_preview_last_deploy_time}",
            f"Last known good SHA: {self.deploy_preview_last_good}",
            f"Preflight status summary: {self._deploy_status_token(preflight_status, len(preflight_status))}  {self._markup_safe(self.deploy_preview_preflight_summary)}",
            "",
        ]
        if self.deploy_preview_info_error:
            lines.append(
                self._style_text(
                    f"PREVIEW NOTE: {self.deploy_preview_info_error}",
                    "bold #ffd700",
                )
            )
            lines.append("")

        lines.extend(
            [
                "DEPLOY PLAN:",
                "  1. Validate mode + scope",
                "  2. Git fetch + checkout <sha>",
                "  3. npm ci",
                "  4. npm run build",
                "  5. pm2 reload",
                "  6. Health check (local + public)",
                "  7. Mark last-known-good",
                "",
                "CONFIRMATION REQUIRED",
                f"  {self.deploy_preview_required_confirm}",
                "",
                "NOTHING HAS EXECUTED YET.",
                "Type the exact confirmation string above to submit the job.",
                "",
            ]
        )
        return lines

    def _panel7b_deploy_job_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        target = self.deploy_job_target or self.deploy_preview_target
        target_code = self._deploy_target_code(target) if target else "-"
        lines = [
            self._line_lr(f"PANEL: 7B  DEPLOY JOB ({target_code})", f"MODE: {state_mode}"),
            self._rule("="),
            "",
            f"DEPLOY STATE: {self.deploy_state}",
        ]
        if self.deploy_job_id:
            lines.append(f"JOB: {self.deploy_job_id}")
        if target:
            lines.append(f"TARGET: {target}")
        if self.deploy_job_running:
            lines.append(self._style_text("STATUS: RUNNING", "bold #ffd700"))
        elif self.deploy_job_result:
            result_style = (
                "bold #00ff00"
                if self.deploy_job_result.status == "OK"
                else ("bold #ffd700" if self.deploy_job_result.status == "WARN" else "bold #ff3030")
            )
            lines.append(self._style_text(f"STATUS: {self.deploy_job_result.status}", result_style))
        lines.extend(["", "JOB OUTPUT:", self._rule("-")])
        if not self.deploy_job_lines:
            lines.append("  Waiting for job output...")
        else:
            for item in self.deploy_job_lines[-24:]:
                lines.append(f"  {item}")
        if self.deploy_job_result and self.deploy_job_result.status != "OK":
            target_for_hint = self.deploy_job_target or self.deploy_preview_target
            lines.extend(
                [
                    "",
                    self._style_text(
                        f"Rollback hint: ROLLBACK {target_for_hint} --to last-good",
                        "bold #ffd700",
                    ),
                ]
            )
        lines.append("")
        return lines

    def _panel8_utilities_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        lines = [
            self._line_lr("PANEL: 8  UTILITIES", f"MODE: {state_mode}"),
            self._rule("="),
            "",
            "  1  Pair CRM",
            "  2  Macros",
            "",
            "COMMANDS:",
            "  1 | 2",
            "  CRM PAIR [base_url] | CRM STATUS | CRM START/NEXT/PAUSE/RESUME/STOP",
            "",
            "CASHLYCRM DEVICE",
            self._rule("-"),
            f"  {self.crm_device_status}",
            "",
        ]
        return lines

    def _panel8_pairing_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        crm_gate = "ALLOW" if self.selected_postauth_state != AppState.OBSERVE else "MAINT REQUIRED"
        gate_style = "bold #00ff00" if crm_gate == "ALLOW" else "bold #ffd700"
        lines = [
            self._line_lr("PANEL: 8A  PAIR CRM", f"MODE: {state_mode}"),
            self._rule("="),
            "",
            "  1  Start CashlyCRM Pairing",
            "  2  CashlyCRM Device Status",
            "",
            "COMMANDS:",
            "  CRM PAIR [base_url]",
            "  CRM STATUS",
            "",
            "POLICY:",
            f"  CRM PAIR gate: {self._style_text(crm_gate, gate_style)}",
            "  Service mode is not required for CashlyCRM pairing.",
            "  Service mode remains TTL-bound for heavier write operations.",
            "",
            "CASHLYCRM DEVICE",
            self._rule("-"),
            f"  {self.crm_device_status}",
            "",
        ]
        if self.crm_pair_running:
            frame = self.deploy_readiness_spinner_frames[
                self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
            ]
            lines.append(self._style_text(f"PAIRING: RUNNING [{frame}]", "bold #ffd700"))
        else:
            result_style = "bold #00ff00" if self.crm_pair_result_status == "OK" else "white"
            lines.append(
                self._style_text(
                    f"PAIRING: {self.crm_pair_result_status}",
                    result_style,
                )
            )
        lines.extend(
            [
                f"STARTED: {self.crm_pair_started_at}",
                self._rule("-"),
            ]
        )
        if not self.crm_pair_lines:
            lines.append("  No pairing run in this session.")
        else:
            for item in self.crm_pair_lines[-18:]:
                lines.append(f"  {item}")
        lines.append("")
        return lines

    def _panel8_macros_lines(self) -> list[str]:
        state_mode = self.selected_postauth_state.value
        macro_gate = "ALLOW" if self.selected_postauth_state != AppState.OBSERVE else "MAINT REQUIRED"
        gate_style = "bold #00ff00" if macro_gate == "ALLOW" else "bold #ffd700"
        specs = autodialer_macro_specs()
        lines = [
            self._line_lr("PANEL: 8B  MACROS", f"MODE: {state_mode}"),
            self._rule("="),
            "",
        ]
        for index, spec in enumerate(specs, start=1):
            lines.append(
                f"  {index:<2} {spec.label:<19} HOTKEY: {autodialer_macro_hotkey(spec.action)}"
            )
        lines.extend(
            [
                f"  {len(specs) + 1:<2} CashlyCRM Device Status",
                "",
                "COMMANDS:",
            ]
        )
        lines.extend(f"  {spec.command}" for spec in specs)
        lines.extend(
            [
                "  CRM STATUS",
                f"  Next-contact shortcut: {next_contact_hotkey()} -> CRM NEXT CONTACT",
                "  Macro hotkeys are configurable through CASHLYCTL_HOTKEY_* env vars.",
                "",
                "POLICY:",
                f"  CRM macro gate: {self._style_text(macro_gate, gate_style)}",
                "  Macros require a paired CashlyCRM browser/device session.",
                "  Docker can dispatch commands, but native host runtime registers global hotkeys.",
                "",
                "CASHLYCRM DEVICE",
                self._rule("-"),
                f"  {self.crm_device_status}",
                "",
            ]
        )
        if self.crm_macro_running:
            frame = self.deploy_readiness_spinner_frames[
                self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
            ]
            lines.append(self._style_text(f"MACRO: RUNNING [{frame}]", "bold #ffd700"))
        else:
            result_style = "bold #00ff00" if self.crm_macro_result_status == "OK" else "white"
            lines.append(
                self._style_text(
                    f"MACRO: {self.crm_macro_result_status}",
                    result_style,
                )
            )
        lines.extend(
            [
                f"STARTED: {self.crm_macro_started_at}",
                self._rule("-"),
            ]
        )
        if not self.crm_macro_lines:
            lines.append("  No macro run in this session.")
        else:
            for item in self.crm_macro_lines[-18:]:
                lines.append(f"  {item}")
        lines.append("")
        return lines

    def _deploy_preview_requested_ref(self) -> str:
        if self.deploy_preview_revision:
            return f"REV {self.deploy_preview_revision}"
        if self.deploy_preview_tag:
            return f"TAG {self.deploy_preview_tag}"
        if self.deploy_preview_target_sha and self.deploy_preview_target_sha != "-":
            return f"LATEST ({self.deploy_preview_target_sha})"
        if self.deploy_preview_branch and self.deploy_preview_branch != "-":
            return f"LATEST ({self.deploy_preview_branch})"
        return "-"

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

    def _summarize_crm_device_session(self) -> str:
        session = load_crm_device_session()
        if not session:
            return "NOT PAIRED"
        device_id = session.device.get("id", "-")
        org_id = session.device.get("organizationId", "-")
        return f"PAIRED device={device_id} org={org_id} base={session.base_url}"

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

    def _header_right_host_os(self) -> str:
        return _host_os_header_label(self.host_inspection)

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
        if self.aws_sso_login_running:
            self._set_command_label(self._sso_loading_command_label("LOGIN"))
            return
        if self.aws_sso_status_loading:
            self._set_command_label(self._sso_loading_command_label("STATUS"))
            return
        if self.deploy_readiness_loading:
            self._set_command_label(self._loading_command_label())
            return
        if self.crm_pair_running:
            self._set_command_label(self._crm_loading_command_label())
            return
        if self.crm_macro_running:
            self._set_command_label(self._crm_macro_loading_command_label())
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

    def _sso_loading_command_label(self, phase: str) -> str:
        frame = self.deploy_readiness_spinner_frames[
            self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
        ]
        return f"{self._command_prefix()} [SSO {phase} {frame}] ===>"

    def _crm_loading_command_label(self) -> str:
        frame = self.deploy_readiness_spinner_frames[
            self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
        ]
        return f"{self._command_prefix()} [CRM PAIR {frame}] ===>"

    def _crm_macro_loading_command_label(self) -> str:
        frame = self.deploy_readiness_spinner_frames[
            self.deploy_readiness_spinner_index % len(self.deploy_readiness_spinner_frames)
        ]
        return f"{self._command_prefix()} [CRM MACRO {frame}] ===>"

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

    @staticmethod
    def _redacted_init_admin_audit(init_args: str) -> str:
        args = init_args.strip()
        if not args:
            return "INITADMIN"
        parts = args.split()
        if len(parts) == 1:
            return f"INITADMIN {parts[0]}"
        return f"INITADMIN {parts[0]} ****"

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
    #   CASHLYCTL_NETWORK_TARGETS=crm=https://crm.example.com,worker=https://worker.example.com
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
    #   CASHLYCTL_TARGET_1_NAME=crm
    #   CASHLYCTL_TARGET_1_URL=https://crm.example.com
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


def _runtime_env(key: str, default: str) -> str:
    return runtime_env(key, default)


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
    raw = _runtime_env("CASHLYCTL_DEPLOY_PREFLIGHT_TIMEOUT_SEC", "20").strip()
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


def _host_os_header_label(report: HostInspection) -> str:
    if report.host_os == "unknown" or report.host_os_confidence == "low":
        return ""
    label = report.host_os.upper()
    if report.host_os == "linux" and report.display_server in {"x11", "wayland"}:
        label = f"{label}/{report.display_server.upper()}"
    elif report.is_wsl:
        label = f"{label}/WSL"
    return f"HOST OS = {label}"


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
