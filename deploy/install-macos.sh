#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --help ]]; then
  echo 'Usage: bash deploy/install-macos.sh https://amo.example.com PAIR_CODE "My Mac"'
  exit 0
fi
[[ "$(uname -s)" == Darwin ]] || { echo 'This installer requires macOS.' >&2; exit 1; }
[[ $# -ge 2 ]] || { echo 'Relay HTTPS origin and one-time pairing code are required.' >&2; exit 1; }
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
python_bin=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
    python_bin="$candidate"; break
  fi
done
[[ -n "$python_bin" ]] || { echo 'Install Python 3.12 or newer first.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || "$python_bin" -m venv .venv
.venv/bin/python -m pip install -r requirements-wrapper.txt
if [[ ! -f "$HOME/.amo/device.json" ]]; then
  PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m amo pair "$1" "$2" --name "${3:-My Mac}"
fi
.venv/bin/python deploy/render_macos_service.py
service_file="$HOME/Library/LaunchAgents/io.amo.connector.plist"
service_target="gui/$(id -u)/io.amo.connector"
if launchctl print "$service_target" >/dev/null 2>&1; then
  echo 'Amo is already running. Restart it only after active jobs finish.'
else
  launchctl bootstrap "gui/$(id -u)" "$service_file"
fi
