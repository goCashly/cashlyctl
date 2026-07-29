[CmdletBinding()]
param(
  [string]$Version = "",
  [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptRoot "..\..")
Set-Location $RepoRoot

if (-not $Version) {
  $Version = python -c "import pathlib,tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])"
}

$BuildVenv = Join-Path $RepoRoot ".venv-windows-build"
if (-not (Test-Path $BuildVenv)) {
  python -m venv $BuildVenv
}

$Python = Join-Path $BuildVenv "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install --upgrade pyinstaller
& $Python -m pip install .
& $Python -m PyInstaller --noconfirm --clean (Join-Path $ScriptRoot "cashlyctl.spec")

if ($SkipInstaller) {
  Write-Host "Built executable folder: $RepoRoot\dist\cashlyctl"
  exit 0
}

$Inno = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $Inno) {
  Write-Warning "Inno Setup Compiler (ISCC.exe) was not found. Install Inno Setup or rerun with -SkipInstaller."
  Write-Host "Built executable folder: $RepoRoot\dist\cashlyctl"
  exit 0
}

$env:CASHLYCTL_WINDOWS_VERSION = $Version
Push-Location $ScriptRoot
try {
  & $Inno.Source "cashlyctl.iss"
}
finally {
  Pop-Location
}

Write-Host "Built installer: $RepoRoot\dist\installer\CashlyCTLSetup-$Version.exe"
