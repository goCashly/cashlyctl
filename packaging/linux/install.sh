#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: packaging/linux/install.sh [OPTIONS]

Install cashlyctl as a user-local Linux command.

Options:
  --no-desktop-entry  Do not install the desktop shortcut entry.
  --dry-run           Print actions without changing files.
  -h, --help          Show this help text.

Environment:
  PYTHON                    Python executable to use. Defaults to python3.
  CASHLYCTL_INSTALL_ROOT    Install root. Defaults to ~/.local/share/cashlyctl.
  CASHLYCTL_BIN_DIR         Launcher directory. Defaults to ~/.local/bin.
EOF
}

install_desktop_entry=1
dry_run=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-desktop-entry)
      install_desktop_entry=0
      ;;
    --dry-run)
      dry_run=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/../.." && pwd)"
python_bin="${PYTHON:-python3}"
install_root="${CASHLYCTL_INSTALL_ROOT:-$HOME/.local/share/cashlyctl}"
bin_dir="${CASHLYCTL_BIN_DIR:-$HOME/.local/bin}"
venv_dir="$install_root/venv"
launcher="$bin_dir/cashlyctl"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_entry="$applications_dir/cashlyctl-next-contact.desktop"

run() {
  printf '+ %s\n' "$*"
  if [ "$dry_run" -eq 0 ]; then
    "$@"
  fi
}

write_file() {
  local path="$1"
  local mode="$2"
  shift 2
  printf '+ write %s\n' "$path"
  if [ "$dry_run" -eq 0 ]; then
    mkdir -p -- "$(dirname -- "$path")"
    cat > "$path"
    chmod "$mode" "$path"
  else
    cat > /dev/null
  fi
}

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python was not found: $python_bin" >&2
  exit 1
fi

"$python_bin" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("cashlyctl requires Python 3.11 or newer.")
PY

run mkdir -p -- "$install_root" "$bin_dir"
run "$python_bin" -m venv "$venv_dir"
run "$venv_dir/bin/python" -m pip install --upgrade pip
run "$venv_dir/bin/python" -m pip install "$project_root"

write_file "$launcher" 0755 <<EOF
#!/usr/bin/env sh
exec "$venv_dir/bin/cashlyctl" "\$@"
EOF

if [ "$install_desktop_entry" -eq 1 ]; then
  write_file "$desktop_entry" 0644 <<EOF
[Desktop Entry]
Type=Application
Name=CashlyCTL Next Contact
Comment=Send CashlyCRM next-contact macro through the paired cashlyctl device
Exec=$launcher crm next-contact
Terminal=false
Categories=Utility;
NoDisplay=true
EOF
fi

cat <<EOF

cashlyctl installed.

Launcher:
  $launcher

Try:
  cashlyctl --help
  cashlyctl system inspect-host
  cashlyctl hotkeys status

If '$bin_dir' is not on PATH, add it to your shell profile.
For Linux desktop shortcut fallback, bind Ctrl+N to:
  $launcher crm next-contact
EOF
