#!/usr/bin/env bash
set -euo pipefail

purge_state=0

usage() {
  cat <<'EOF'
Usage: packaging/linux/uninstall.sh [OPTIONS]

Remove the user-local Linux cashlyctl install.

Options:
  --purge-state  Also remove ~/.cashlyctl local state.
  -h, --help     Show this help text.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge-state)
      purge_state=1
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

install_root="${CASHLYCTL_INSTALL_ROOT:-$HOME/.local/share/cashlyctl}"
bin_dir="${CASHLYCTL_BIN_DIR:-$HOME/.local/bin}"
launcher="$bin_dir/cashlyctl"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_entry="$applications_dir/cashlyctl-next-contact.desktop"

if [ -f "$launcher" ] && grep -q "$install_root/venv/bin/cashlyctl" "$launcher"; then
  rm -f -- "$launcher"
  echo "removed launcher: $launcher"
else
  echo "launcher not removed: $launcher"
fi

rm -f -- "$desktop_entry"
echo "removed desktop entry: $desktop_entry"

rm -rf -- "$install_root"
echo "removed install root: $install_root"

if [ "$purge_state" -eq 1 ]; then
  rm -rf -- "$HOME/.cashlyctl"
  echo "removed state: $HOME/.cashlyctl"
else
  echo "kept state: $HOME/.cashlyctl"
fi
