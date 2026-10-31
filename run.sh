#!/bin/bash
# One-command start: sets up what's missing, then runs the app and opens it in the browser.
set -euo pipefail
cd "$(dirname "$0")"

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

# Anthropic API key for AI analysis: saved from the app (AI analysis → API key) into the macOS Keychain,
# never into a file. Loaded here so the app has it after a restart.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  ANTHROPIC_API_KEY=$(security find-generic-password -s ff680w-anthropic -w 2>/dev/null || true)
  [ -n "$ANTHROPIC_API_KEY" ] && export ANTHROPIC_API_KEY
fi

PORT="${FFSCAN_PORT:-8680}"
(sleep 2 && open "http://localhost:$PORT") &
exec .venv/bin/python -m ffscan.app
