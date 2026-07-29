# CashlyCTL Windows Setup

Use this guide to install CashlyCTL on Windows and enable CashlyCRM autodialer keyboard shortcuts.

## Download

Download the latest internal Windows installer:

https://github.com/goCashly/cashlyctl/releases/download/windows-latest/CashlyCTLSetup-latest.exe

This installer is currently unsigned. Windows SmartScreen may show a warning. Choose **More info** and **Run anyway** only if you trust this Cashly build.

## Install

1. Run `CashlyCTLSetup-latest.exe`.
2. Keep the default install location.
3. Leave the PATH option enabled.
4. Optionally enable **Start CashlyCTL hotkeys when I sign in**.
5. Finish the installer.
6. Open a new Windows Terminal or PowerShell window.

Verify the command is available:

```powershell
cashlyctl --help
```

If Windows cannot find `cashlyctl`, close and reopen the terminal. You can also run it directly from:

```text
%LOCALAPPDATA%\Programs\CashlyCTL\cashlyctl.exe
```

## Create A Local CashlyCTL Admin

Start the console:

```powershell
cashlyctl console
```

In the CashlyCTL console, run:

```text
INITADMIN
```

Follow the prompts to create a local admin username and password. This is local CashlyCTL authentication only; it does not replace your CashlyCRM login.

## Pair With CashlyCRM

In the CashlyCTL console:

```text
LOGON
SET STATE MAINT
=8
1
CRM PAIR
```

CashlyCTL will show a pairing code and should open your browser. If the browser does not open, copy the displayed URL manually.

Approve the code in your logged-in CashlyCRM browser session, then return to CashlyCTL.

Verify the device pairing from PowerShell:

```powershell
cashlyctl crm status
cashlyctl crm whoami
```

## Test A Macro

Open CashlyCRM, start or open the autodialer page, then test the command path:

```powershell
cashlyctl crm next-contact
```

The command should work even if the CashlyCRM browser tab is inactive or minimized, as long as the paired browser session is open and polling commands.

## Start Keyboard Shortcuts

Check the configured shortcut bindings:

```powershell
cashlyctl hotkeys start --dry-run
```

Start the native Windows hotkey helper:

```powershell
cashlyctl hotkeys start
```

Keep that helper running. The terminal window can be minimized.

Default shortcuts:

```text
Ctrl+G  -> start autodialer
Ctrl+N  -> next contact
Ctrl+P  -> pause autodialer
Ctrl+R  -> resume autodialer
Ctrl+X  -> stop autodialer
```

## Troubleshooting

- If `cashlyctl` is not recognized, open a new terminal or run `%LOCALAPPDATA%\Programs\CashlyCTL\cashlyctl.exe`.
- If pairing fails, make sure you are logged in to CashlyCRM in the browser before approving the code.
- If a hotkey does nothing, run `cashlyctl crm next-contact` manually first to confirm pairing and command dispatch.
- If a shortcut cannot register, another Windows app may already own it. Change the binding with the `CASHLYCTL_HOTKEY_*` environment variables.
- If the CashlyCRM tab was closed, reopen CashlyCRM and the autodialer page before using macros.

## For AI Assistants

If an AI assistant cannot read the GitHub branch page, give it this raw Markdown URL:

https://raw.githubusercontent.com/goCashly/cashlyctl/cashlyctl-windows/WINDOWS_SETUP.md
