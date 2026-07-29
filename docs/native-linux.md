# Native Linux Install

Status: initial implementation  
Scope: user-local Linux install for `cashlyctl`

## Install

### Arch Linux / AUR

Install the rolling Git package:

```bash
yay -S cashlyctl-git
```

Or with another AUR helper:

```bash
paru -S cashlyctl-git
```

Then verify:

```bash
cashlyctl system inspect-host
cashlyctl hotkeys status
```

The AUR package tracks the public `main` branch. The package recipe is included
in this repository under `packaging/aur/cashlyctl-git/`; publishing requires an
AUR account with an SSH key registered for `aur@aur.archlinux.org`.

### Manual User-Local Install

From the `cashlyctl-src` checkout:

```bash
packaging/linux/install.sh
```

The installer does not use `sudo`. It creates:

- `~/.local/share/cashlyctl/venv`
- `~/.local/bin/cashlyctl`
- `~/.local/share/applications/cashlyctl-next-contact.desktop`

If `~/.local/bin` is not on `PATH`, add it in your shell profile.

## Verify

```bash
cashlyctl --help
cashlyctl system inspect-host
cashlyctl hotkeys status
cashlyctl console
```

## CashlyCRM Pairing

```bash
cashlyctl console
```

Then in the console:

```text
INITADMIN
LOGON
SET STATE MAINT
=8
1
CRM PAIR
```

The paired CRM device token is stored under `~/.cashlyctl/auth/`.

## Desktop Shortcut Fallback

The native hotkey daemon is the next implementation phase. Until then, Linux desktop environments can bind a global shortcut to the installed command.

Suggested bindings:

```text
Ctrl+G  -> cashlyctl crm start
Ctrl+N  -> cashlyctl crm next-contact
Ctrl+P  -> cashlyctl crm pause
Ctrl+R  -> cashlyctl crm resume
Ctrl+X  -> cashlyctl crm stop
```

Suggested next-contact command:

```bash
~/.local/bin/cashlyctl crm next-contact
```

This works best for GNOME, KDE, Cinnamon, XFCE, and other desktops with custom keyboard shortcut settings. On Wayland, this desktop shortcut fallback is more reliable than trying to capture global keystrokes directly.

## Uninstall

```bash
packaging/linux/uninstall.sh
```

To remove local `cashlyctl` state too:

```bash
packaging/linux/uninstall.sh --purge-state
```
