# cashlyctl

CashlyCTL is the local operations companion for CashlyCRM and Cashly deployment workflows. It provides a terminal console, a scriptable CLI, local user authentication, browser-approved CashlyCRM pairing, host diagnostics, and authenticated autodialer macro controls.

Use CashlyCTL to:

- Pair a local device with a logged-in CashlyCRM browser session.
- Send CashlyCRM autodialer commands from the terminal or native hotkeys.
- Run host, network, and deployment readiness diagnostics.
- Keep local operator state under the current user's profile.
- Support native Windows/Linux installs and Docker-based operations.

## Developer Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cashlyctl console
```

On first run, start the console and type `INITADMIN` to create the first local admin user.

## CLI

```bash
cashlyctl console
cashlyctl profile list
cashlyctl health
cashlyctl health --profile local-dev
cashlyctl system inspect-host
cashlyctl crm pair
cashlyctl crm whoami
cashlyctl crm start
cashlyctl crm next-contact
cashlyctl crm pause
cashlyctl crm resume
cashlyctl crm stop
cashlyctl hotkeys status
cashlyctl hotkeys start --dry-run
```

## Native Linux

Arch Linux / AUR:

The AUR recipe is prepared under `packaging/aur/cashlyctl-git/`. After the
package is published to AUR:

```bash
yay -S cashlyctl-git
cashlyctl system inspect-host
cashlyctl hotkeys status
```

Manual user-local install:

```bash
packaging/linux/install.sh
cashlyctl system inspect-host
cashlyctl hotkeys status
```

The manual installer creates a user-local venv under
`~/.local/share/cashlyctl` and a launcher at `~/.local/bin/cashlyctl`. See
[Native Linux](docs/native-linux.md).

## Windows Install And Setup

Full setup guide:

[WINDOWS_SETUP.md](WINDOWS_SETUP.md)

AI assistants that cannot read GitHub's web page can use the raw Markdown guide:

https://raw.githubusercontent.com/goCashly/cashlyctl/cashlyctl-windows/WINDOWS_SETUP.md

1. Download the latest Windows installer:

[Download CashlyCTLSetup-latest.exe](https://github.com/goCashly/cashlyctl/releases/download/windows-latest/CashlyCTLSetup-latest.exe)

The current internal installer is unsigned. Windows may show a SmartScreen warning; choose **More info** and **Run anyway** only if you trust this Cashly build.

2. Run `CashlyCTLSetup-latest.exe`.

The installer adds `cashlyctl.exe` to your user PATH, creates Start Menu shortcuts, and can optionally add CashlyCTL Hotkeys to Startup Apps.

3. Open a new Windows Terminal or PowerShell window.

4. Create the first local CashlyCTL admin user:

```powershell
cashlyctl console
```

Inside the console:

```text
INITADMIN
```

5. Pair CashlyCTL with CashlyCRM:

```text
LOGON
SET STATE MAINT
=8
1
CRM PAIR
```

Approve the displayed code in your logged-in CashlyCRM browser.

6. Verify the paired device from PowerShell:

```powershell
cashlyctl crm status
cashlyctl crm whoami
```

7. Test the CRM macro command path:

```powershell
cashlyctl crm next-contact
```

8. Start native Windows hotkeys:

```powershell
cashlyctl hotkeys start
```

Default Windows hotkeys:

```text
Ctrl+G  -> start autodialer
Ctrl+N  -> next contact
Ctrl+P  -> pause autodialer
Ctrl+R  -> resume autodialer
Ctrl+X  -> stop autodialer
```

The terminal running `cashlyctl hotkeys start` can be minimized. CashlyCRM can be inactive or minimized as long as the paired, logged-in browser session remains open.

If the installer link is not available yet, check the latest branch build:

[Windows installer builds](https://github.com/goCashly/cashlyctl/actions/workflows/windows-installer.yml?query=branch%3Acashlyctl-windows)

For packaging details, see [Native Windows](docs/native-windows.md).

## Docker

```bash
docker build -t cashlyctl:local .
docker run --rm cashlyctl:local --help
docker run --rm cashlyctl:local system inspect-host
docker run --rm -it -v cashlyctl-state:/home/cashly/.cashlyctl -v "$PWD/.env:/home/cashly/.cashlyctl/.env:ro" cashlyctl:local console
docker run --rm -it -v cashlyctl-state:/home/cashly/.cashlyctl cashlyctl:local console
```

See [Docker](docs/docker.md) for persistent state, env-file, and networking notes.

## Design Docs

- [Global hotkey companion](docs/global-hotkey-companion.md): authenticated system-wide hotkeys for CashlyCRM workflows, host OS detection, and Docker packaging strategy.
- [CashlyCRM auth pairing](docs/cashlycrm-auth.md): browser-approved device auth for local CashlyCTL installs.
- [Native Linux](docs/native-linux.md): user-local Linux installer and desktop shortcut fallback.
- [Native Windows](docs/native-windows.md): Windows global hotkey helper and single-installer packaging.

## Local State

`cashlyctl` stores files under `~/.cashlyctl/` on Linux/macOS and
`%LOCALAPPDATA%\CashlyCTL\` on Windows:

- `config.toml`
- `catalog/`
- `queries/`
- `state/`
- `logs/`

On first run, it creates a starter config with local and remote profile examples.

## Login Credentials

Local console users are stored under `auth/local_users.json` as salted password hashes. On first run, create the local admin from inside the console:

- `INITADMIN`
- `INITADMIN <username>`

Legacy/dev login credentials can still be loaded from `.env` entries:

- `CASHLYCTL_LOGIN_ADMIN=<password>`

Command usage in console:

- `LOGON`
- `L`

## CashlyCRM Auth

CashlyCRM pairing is a maintenance-mode operation. In the console:

- `LOGON`
- `SET STATE MAINT`
- `CRM PAIR`

The CLI equivalent is `cashlyctl crm pair`, which first prompts for the local CashlyCTL admin user, then asks the user to approve the pairing in their logged-in CashlyCRM browser.

## Network / AWS Env Config

`cashlyctl` now reads network target and AWS drilldown config from environment variables (including `.env` file values).

Network target config:

- Compact: `CASHLYCTL_NETWORK_TARGETS=name1=https://url1,name2=https://url2`
- Indexed:
- `CASHLYCTL_TARGET_1_NAME=...`
- `CASHLYCTL_TARGET_1_URL=...`

AWS drilldown config (optional):

- `CASHLYCTL_AWS_REGION=us-east-1`
- Per target overrides:
- `CASHLYCTL_AWS_REGION_<TARGET_KEY>`
- `CASHLYCTL_AWS_INSTANCE_ID_<TARGET_KEY>`
- `CASHLYCTL_AWS_INSTANCE_NAME_<TARGET_KEY>`
- `CASHLYCTL_AWS_TARGET_GROUP_<TARGET_KEY>`

`<TARGET_KEY>` is target name uppercased with non-alphanumeric chars replaced by `_`.

Deployment SSH runner config (Panel 7):

- `CASHLYCTL_DEPLOY_<TARGET_KEY>_HOST`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_USER`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_PORT` (default `22`)
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_SSH_KEY_PATH` (optional local PEM/private key path for `ssh -i`)
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_APP_DIR`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_PM2_PROCESS`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_HEALTH_LOCAL`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_HEALTH_PUBLIC`
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_DEFAULT_REF` (default `origin/main`)
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_NGINX_RELOAD` (`0/1`)
- `CASHLYCTL_DEPLOY_<TARGET_KEY>_LAST_GOOD`
- `CASHLYCTL_DEPLOY_PREFLIGHT_TIMEOUT_SEC` (default `10`)

Notes:

- `cashlyctl` only needs SSH access to the EC2 host.
- Git deploy keys for app repos should remain on the EC2 host/user that runs `git fetch`.

Optional ASCII logo settings via env vars:

- `CASHLYCTL_ASCII_TEXT=cashlyCTL`
- `CASHLYCTL_ASCII_FONT=slant`

## Security

See [SECURITY.md](SECURITY.md). Report vulnerabilities privately to security@gocashly.io.

## License

Copyright (c) 2026 Cashly Tech Services Inc. See [LICENSE](LICENSE).
