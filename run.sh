#!/bin/bash
# One-command start: sets up what's missing, then runs the app and opens it in the browser.
set -euo pipefail
cd "$(dirname "$0")"

# One ffscan at a time: if it's already running, show it (its window, or the page in the browser) and stop here.
if curl -s -o /dev/null "http://localhost:${FFSCAN_PORT:-8680}" && [ -x .venv/bin/python ]; then
  exec .venv/bin/python -c "from ffscan.desktop import show_running; show_running()"
fi

missing=()
for tool in exiftool rclone; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
if ((${#missing[@]})); then
  echo "Missing: ${missing[*]}  ->  install with:  brew install ${missing[*]}" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python environment..."
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

if [ ! -x tools/orient ] || [ tools/orient.swift -nt tools/orient ]; then
  echo "Building auto-rotate helper..."
  swiftc -O tools/orient.swift -o tools/orient
fi

# --window: ffscan in its own window (FFScan.command); closing it stops the app.
if [ "${1:-}" = "--window" ]; then
  .venv/bin/pip install -q -r requirements-desktop.txt
  exec .venv/bin/python -m ffscan.desktop
fi

PORT="${FFSCAN_PORT:-8680}"
# Open the browser once the server answers (first start can take a while).
(until curl -s -o /dev/null "http://localhost:$PORT"; do sleep 0.5; done; open "http://localhost:$PORT") &
exec .venv/bin/python -m ffscan.app
