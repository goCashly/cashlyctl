# cashlyctl Global Hotkey Companion

Status: draft  
Scope: `cashlyctl-src`, CashlyCRM browser app, CashlyCRM production command broker  
Created: 2026-07-28

## Purpose

Cashly users need system-wide hotkeys for live CRM workflows. The first target workflow is campaign autodialer control: while a campaign is running in CashlyCRM, a user should be able to press a global shortcut, even when the browser tab is inactive or minimized, and advance to the next contact.

The clean implementation is not browser key injection. `cashlyctl` should become an authenticated local companion that captures trusted host hotkeys and sends named commands to `crm.gocashly.io`. The production CRM brokers those commands to the user's active browser session, where the existing campaign calling code executes the action.

## Goals

- Support Windows, macOS, and Linux.
- Support inactive or minimized CashlyCRM browser tabs when the tab is still alive.
- Keep commands authenticated, scoped, auditable, and revocable.
- Send named commands such as `campaign.next_contact`, not arbitrary keystrokes.
- Provide a Docker image for non-hotkey commands, diagnostics, pairing, and command dispatch.
- Detect the local runtime and host OS well enough to guide installation, hotkey backend selection, and Docker limitations.

## Non-Goals

- Do not automate Chrome/Safari/Edge by sending synthetic browser keystrokes.
- Do not rely on a focused terminal for production hotkey behavior.
- Do not make `cashlyctl` a general keylogger or macro recorder.
- Do not make Docker the only runtime for global hotkeys. Containers cannot reliably register host OS global shortcuts across Windows, macOS, and Linux.

## Product Shape

Primary commands:

```bash
cashlyctl crm pair
cashlyctl hotkeys start
cashlyctl hotkeys status
cashlyctl hotkeys stop
cashlyctl hotkeys doctor
cashlyctl crm next-contact
cashlyctl crm pause
cashlyctl crm stop
cashlyctl system inspect-host
cashlyctl system inspect-host --json
```

Default hotkeys:

```toml
[hotkeys]
autodialer_start = "Ctrl+G"
next_contact = "Ctrl+N"
autodialer_pause = "Ctrl+P"
autodialer_resume = "Ctrl+R"
autodialer_stop = "Ctrl+X"
```

The shortcuts must be user-configurable because OS and desktop environments can reserve or reject specific combinations.

## High-Level Architecture

```text
Host OS
  cashlyctl hotkey companion
    - detects host/runtime
    - registers global shortcuts when supported
    - opens authenticated WSS connection to crm.gocashly.io
    - sends named commands

crm.gocashly.io
  cashlyctl command broker
    - validates CLI token
    - authorizes user/org/action
    - routes command to active browser session
    - records audit event and acknowledgement

CashlyCRM browser tab
  campaign command receiver
    - registers active campaign session
    - receives command
    - invokes existing campaign action
    - returns applied/ignored/error acknowledgement
```

## Command Contract

The CLI should emit a command envelope with a stable idempotency key.

```json
{
  "id": "cmd_01J...",
  "type": "campaign.next_contact",
  "issuedAt": "2026-07-28T14:00:00.000Z",
  "source": "cashlyctl",
  "deviceId": "ctl_dev_...",
  "userId": "user_...",
  "organizationId": "org_...",
  "target": {
    "kind": "active_campaign_calling_session",
    "campaignId": "campaign_..."
  },
  "payload": {}
}
```

Browser acknowledgement:

```json
{
  "id": "cmd_01J...",
  "status": "applied",
  "appliedAt": "2026-07-28T14:00:00.250Z",
  "detail": "advanced_to_next_contact"
}
```

Valid acknowledgement statuses:

- `applied`
- `ignored`
- `rejected`
- `failed`

Common ignored/rejected reasons:

- `campaign_not_running`
- `no_active_browser_session`
- `no_active_campaign_session`
- `unauthorized_campaign`
- `call_in_progress_requires_hangup`
- `shortcut_already_registered`

## Authentication And Pairing

Use a browser pairing flow instead of collecting CRM credentials in the terminal.

```text
1. User runs: cashlyctl crm pair
2. CLI requests a device code from crm.gocashly.io.
3. CLI displays a URL and short code.
4. User approves in an already authenticated browser session.
5. CLI receives a scoped device token and stores it under ~/.cashlyctl/.
6. CLI uses that token for HTTPS/WSS command broker access.
```

Token properties:

- Scoped to `cashlyctl:commands`.
- Bound to user, organization, device id, and optional workstation label.
- Revocable from CashlyCRM settings.
- Short-lived access token with refresh token or renewable device session.
- Stored using the host keychain where practical; fall back to a restricted file in `~/.cashlyctl/`.

## Server Endpoints

The exact route host can be Next.js or `CashlyCRM-Server`, but websocket upgrades should live where production infrastructure already supports upgrades reliably.

Suggested endpoints:

```text
POST /api/cashlyctl/pair/start
POST /api/cashlyctl/pair/complete
POST /api/cashlyctl/pair/poll
DELETE /api/cashlyctl/devices/:deviceId
GET /api/cashlyctl/ws
POST /api/cashlyctl/commands
```

The WebSocket is preferred for the hotkey companion. `POST /api/cashlyctl/commands` exists for single-shot commands and Linux desktop shortcut fallback.

## Browser Integration

The browser should execute the command because the campaign autodialer currently owns live state in React and Twilio Voice SDK context.

Implementation sketch:

- Add a `useCashlyCtlCommandReceiver` hook.
- Mount it near the global campaign modal/provider.
- Register active campaign calling sessions with the command broker.
- On `campaign.next_contact`, call the same path as the UI skip button.
- If a call is active, decide the safe behavior explicitly:
  - MVP: reject with `call_in_progress_requires_hangup`.
  - Later: add `campaign.safe_next_contact` that hangs up, records the outcome, then advances.

Existing CashlyCRM code already has the right local action surface:

- `useCampaignCalling(...).actions.skipContact()`
- `InitiateCampaignModalDraggable` already wraps the global campaign calling workflow.

## Host OS And Runtime Detection

Add a small host inspection module that returns a structured report.

```json
{
  "runtimeOs": "linux",
  "runtimeArch": "x86_64",
  "hostOs": "windows",
  "hostOsConfidence": "medium",
  "sessionType": "wsl2",
  "displayServer": null,
  "isContainer": false,
  "isWsl": true,
  "hotkeySupport": "external_shortcut_only",
  "recommendedBackend": "windows_native_helper"
}
```

Detection inputs:

- Python `platform.system()`, `platform.machine()`, and `sys.platform`.
- Environment overrides:
  - `CASHLYCTL_HOST_OS=windows|macos|linux`
  - `CASHLYCTL_HOTKEY_BACKEND=auto|windows|macos|x11|wayland_portal|desktop_shortcut|none`
- Container signals:
  - `/.dockerenv`
  - `/proc/1/cgroup`
  - `container`, `KUBERNETES_SERVICE_HOST`, and similar env markers
- WSL signals:
  - `/proc/version` containing Microsoft or WSL
  - `WSL_DISTRO_NAME`
- Linux desktop signals:
  - `XDG_SESSION_TYPE=x11|wayland`
  - `WAYLAND_DISPLAY`
  - `DISPLAY`
- Docker host hints:
  - explicit `CASHLYCTL_HOST_OS`
  - generated config mounted into the container
  - Docker Desktop environment markers where present

Important limitation: a container can reliably detect its own runtime OS. It cannot always infer the host OS, especially under Docker Desktop, without an explicit host hint. `cashlyctl system inspect-host` should show confidence and explain when an override is needed.

## Global Hotkey Strategy

Use platform-aware backends behind a shared interface.

```python
class HotkeyBackend(Protocol):
    def describe_capabilities(self) -> HotkeyCapabilities: ...
    def register(self, bindings: dict[str, str], handler: HotkeyHandler) -> None: ...
    def run_forever(self) -> None: ...
    def stop(self) -> None: ...
```

Backend matrix:

| Platform | Preferred Backend | Notes |
|---|---|---|
| Windows | Native `RegisterHotKey` helper | No admin required for normal shortcuts. Registration can fail if another app owns the shortcut. |
| macOS | Native packaged helper or accessibility-approved listener | User may need Accessibility/Input Monitoring approval depending on implementation and shortcut type. |
| Linux X11 | X11 global shortcut registration | Requires active X session and `DISPLAY`. |
| Linux Wayland | Desktop portal when available | Support varies by compositor and desktop environment. |
| Linux Wayland fallback | User-configured desktop shortcut | Most reliable fallback: bind the desktop shortcut to `cashlyctl crm next-contact`. |
| Docker container | No direct host hotkeys | Use native host companion, desktop shortcut invoking Docker, or single-shot command dispatch. |

For MVP speed, a Python listener library can prove the command loop. For production, prefer native global shortcut registration over raw keyboard monitoring so the process behaves like a shortcut owner, not a key capture tool.

## Docker Strategy

Docker is useful for repeatable install and diagnostics, but it should not be the only supported runtime for global hotkeys.

Supported Docker use cases:

```bash
docker run --rm ghcr.io/cashly/cashlyctl:latest health
docker run --rm -it -v cashlyctl-state:/home/cashly/.cashlyctl ghcr.io/cashly/cashlyctl:latest crm pair
docker run --rm -v cashlyctl-state:/home/cashly/.cashlyctl ghcr.io/cashly/cashlyctl:latest crm next-contact
docker run --rm ghcr.io/cashly/cashlyctl:latest system inspect-host
```

Unsupported or limited Docker use cases:

- Registering global hotkeys directly from a container on macOS or Windows Docker Desktop.
- Registering global hotkeys directly from a container on Wayland Linux.
- Detecting the host OS with high confidence without an explicit host hint.

Docker implementation requirements:

- Multi-arch images: `linux/amd64` and `linux/arm64`.
- Non-root user by default.
- Persistent state volume for `~/.cashlyctl/`.
- `CASHLYCTL_HOST_OS` override for host-aware diagnostics.
- `cashlyctl hotkeys doctor` should explain when Docker cannot provide host hotkeys.

Recommended packaging model:

- Native install for `cashlyctl hotkeys start`.
- Docker install for operational commands, CI, pairing validation, and emergency command dispatch.
- Optional desktop shortcut fallback that invokes either native `cashlyctl crm next-contact` or a Dockerized single-shot command.

## Data Model

Suggested tables:

```text
cashlyctl_devices
  id
  user_id
  organization_id
  label
  platform
  public_key_or_token_hash
  scopes
  created_at
  last_seen_at
  revoked_at

cashlyctl_command_audit
  id
  device_id
  user_id
  organization_id
  command_type
  target_kind
  target_id
  status
  reason
  issued_at
  acknowledged_at
  created_at
```

Avoid storing raw access tokens. Store token hashes, public keys, or encrypted refresh material as appropriate for the chosen auth design.

## Implementation Phases

### Phase 1: Command Dispatch Without Hotkeys

- Add `cashlyctl crm pair`.
- Add token storage.
- Add `cashlyctl crm next-contact`.
- Add server command broker endpoint.
- Add browser receiver and acknowledgement.
- Wire `campaign.next_contact` to the existing campaign skip action.

Acceptance:

- Running `cashlyctl crm next-contact` advances the active browser campaign session.
- Command is ignored with a clear reason when no campaign is active.
- Command audit records exist.

### Phase 2: Long-Running Companion

- Add `cashlyctl hotkeys start/status/stop`.
- Add WSS connection management with reconnect and heartbeat.
- Add local config for hotkey bindings.
- Add command acknowledgement display in terminal logs.

Acceptance:

- A focused terminal is not required after the companion is running.
- Command delivery survives brief network disconnects.
- Duplicate command ids are idempotent.

### Phase 3: Native Hotkey Backends

- Implement Windows backend with `RegisterHotKey`.
- Implement macOS backend with clear permission diagnostics.
- Implement Linux X11 backend.
- Implement Linux Wayland portal or detect unsupported portal state.
- Implement desktop shortcut fallback guidance.

Acceptance:

- `cashlyctl hotkeys doctor` gives accurate backend status on each platform.
- Shortcut registration failure is visible and actionable.
- Wayland fallback generates exact commands for GNOME/KDE/custom bindings.

### Phase 4: Docker Image

- Add Dockerfile.
- Add image build workflow.
- Add `cashlyctl system inspect-host`.
- Add documentation for host OS overrides and persistent state volumes.

Acceptance:

- Docker image runs health, pair, command dispatch, and inspect-host.
- Dockerized `hotkeys start` exits with clear guidance unless a supported host integration is explicitly configured.
- Native Linux installs can distinguish X11, Wayland, SSH/headless sessions, WSL, and containerized runtimes.

### Phase 5: Hardening

- Add device revocation UI.
- Add per-command authorization checks.
- Add rate limits and command size limits.
- Add structured audit logging.
- Add production dashboards for connected devices and command failures.

Acceptance:

- Security can answer who sent which command, from which device, to which campaign, and whether the browser applied it.
- Revoking a device immediately prevents new WSS and HTTPS command usage.

## Open Decisions

- Whether the command broker lives in Next.js, `CashlyCRM-Server`, or a dedicated realtime service.
- Whether the browser command channel reuses existing WebSocket infrastructure or gets a separate CashlyCtl channel.
- Whether `next-contact` should hang up active calls or only skip during countdown/idle state.
- Which production-native backend stack to standardize on:
  - Python platform-specific backends.
  - A small Rust/Tauri hotkey helper invoked by Python.
  - Separate native installers per OS.

## External References

- Electron `globalShortcut`: https://www.electronjs.org/docs/latest/api/global-shortcut
- Tauri global shortcut plugin: https://tauri.app/reference/javascript/global-shortcut/
- pynput platform limitations: https://pynput.readthedocs.io/en/latest/limitations.html
