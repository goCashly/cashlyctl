# CashlyCRM Auth Pairing

CashlyCTL uses browser-approved device pairing for CashlyCRM access.

## Mode policy

- `OBSERVE`: view-only; `CRM PAIR` is blocked.
- `MAINT`: allowed; use this for CashlyCRM pairing and local device setup.
- `SERVICE`: still reserved for heavier write operations and keeps its TTL window.

Pairing is a maintenance operation because it creates local auth state, but it does not need the service write window.

## Console flow

```text
LOGON
SET STATE MAINT
=8
1
CRM PAIR
```

CashlyCTL displays a browser URL and pairing code. The user opens the URL, signs in to CashlyCRM if needed, approves the code, and returns to the terminal.

Native installs try to open the browser automatically. Docker/container runs print the URL and an explicit auto-open skip message because a container cannot reliably launch the host desktop browser.

Utilities now opens as a small chooser:

- `1 Pair CRM`: pairing and device status.
- `2 Macros`: browser command macros such as next contact.

## CLI flow

```bash
cashlyctl crm pair
cashlyctl crm whoami
cashlyctl crm next-contact
```

`crm pair` prompts for the local CashlyCTL admin user first. This is the local gate; the CashlyCRM approval still happens in the browser with the user's normal CRM session.

Use `--open-browser` for a best-effort browser launch in native installs:

```bash
cashlyctl crm pair --open-browser
```

## Docker persistence

Device auth is stored under `$CASHLYCTL_HOME/auth/cashlycrm_device.json`. With Docker, keep the same volume mounted across runs:

```bash
docker run --rm -it \
  -v cashlyctl-home:/home/cashly/.cashlyctl \
  cashlyctl:local crm pair
```

The server stores only hashed device tokens. The raw token is shown to CashlyCTL once during polling and then saved locally.

## Next-contact macro

`CRM NEXT CONTACT` queues an `autodialer.next_contact` command through the paired device token. A running CashlyCRM campaign calling tab polls for pending commands, skips the active contact, starts the next contact, and acknowledges the command.

The default planned host shortcut is `Ctrl+Shift+S`. Override the displayed binding with `CASHLYCTL_HOTKEY_NEXT_CONTACT` while we build the native hotkey companion.

This is the command path that future system-wide hotkeys should call.
