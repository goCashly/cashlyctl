# Native Windows Install

Status: initial implementation

Scope: Windows terminal command, native global hotkey helper, and installer packaging

## Runtime Behavior

The Windows helper registers system-wide hotkeys with the Win32 `RegisterHotKey` API. When a shortcut fires, it sends the same paired CashlyCRM command used by the normal CLI:

```powershell
cashlyctl crm start
cashlyctl crm next-contact
cashlyctl crm pause
cashlyctl crm resume
cashlyctl crm stop
```

The terminal window can be minimized while `cashlyctl hotkeys start` is running. CashlyCRM can be inactive or minimized as long as the paired, logged-in browser session is still open and polling commands.

## Download

Download the latest unsigned installer built from the `cashlyctl-windows` branch:

[CashlyCTLSetup-latest.exe](https://github.com/goCashly/cashlyctl/releases/download/windows-latest/CashlyCTLSetup-latest.exe)

If that link has not been published yet, use the latest GitHub Actions artifact:

[Windows installer builds](https://github.com/goCashly/cashlyctl/actions/workflows/windows-installer.yml?query=branch%3Acashlyctl-windows)

## User Install Flow

After installing:

```powershell
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

Start the hotkey helper:

```powershell
cashlyctl hotkeys start
```

To check bindings without registering them:

```powershell
cashlyctl hotkeys start --dry-run
```

Default bindings:

```text
Ctrl+G  -> cashlyctl crm start
Ctrl+N  -> cashlyctl crm next-contact
Ctrl+P  -> cashlyctl crm pause
Ctrl+R  -> cashlyctl crm resume
Ctrl+X  -> cashlyctl crm stop
```

Override bindings with environment variables:

```powershell
$env:CASHLYCTL_HOTKEY_AUTODIALER_START = "Ctrl+Alt+G"
$env:CASHLYCTL_HOTKEY_NEXT_CONTACT = "Ctrl+Alt+N"
$env:CASHLYCTL_HOTKEY_AUTODIALER_PAUSE = "Ctrl+Alt+P"
$env:CASHLYCTL_HOTKEY_AUTODIALER_RESUME = "Ctrl+Alt+R"
$env:CASHLYCTL_HOTKEY_AUTODIALER_STOP = "Ctrl+Alt+X"
```

## Local State

On Windows, local state defaults to:

```text
%LOCALAPPDATA%\CashlyCTL
```

This includes local CashlyCTL users and the paired CashlyCRM device token. Set `CASHLYCTL_HOME` to override this location.

## Build

Build from a Windows machine with Python 3.11+:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

The script builds:

- `dist\cashlyctl\cashlyctl.exe`
- `dist\installer\CashlyCTLSetup-<version>.exe` when Inno Setup is installed

Use this to build only the executable folder:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -SkipInstaller
```

The installer is user-level, installs under `%LOCALAPPDATA%\Programs\CashlyCTL`, adds the install directory to the user PATH, creates Start Menu shortcuts, and can optionally add CashlyCTL Hotkeys to Startup Apps.
