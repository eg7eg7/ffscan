#!/bin/bash
# Puts "FFScan.app" on the Desktop: double-click it to start ffscan (if it isn't running) and open it in the browser.
# Re-run after moving this folder, since the app remembers where ffscan lives.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$HOME/Desktop/FFScan.app}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>FFScan</string>
  <key>CFBundleIdentifier</key><string>io.github.ffscan.launcher</string>
  <key>CFBundleExecutable</key><string>FFScan</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
EOF

cat > "$APP/Contents/MacOS/FFScan" <<EOF
#!/bin/bash
# Apps started from Finder don't get the shell's PATH: add Homebrew's.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
PORT="\${FFSCAN_PORT:-8680}"
URL="http://localhost:\$PORT"
LOG="\$HOME/Library/Logs/ffscan.log"
if curl -s -o /dev/null "\$URL"; then open "\$URL"; exit 0; fi
nohup "$REPO/run.sh" >"\$LOG" 2>&1 &
pid=\$!
# run.sh opens the browser itself; just report if it gives up before the server answers.
until curl -s -o /dev/null "\$URL"; do
  if ! kill -0 \$pid 2>/dev/null; then
    msg=\$(tail -n 5 "\$LOG" | tr '"\\\\' "'/")
    osascript -e "display dialog \"FFScan could not start.\\n\\n\$msg\\n\\nFull log: \$LOG\" with title \"FFScan\" buttons {\"OK\"} with icon stop"
    exit 1
  fi
  sleep 1
done
EOF
chmod +x "$APP/Contents/MacOS/FFScan"

# Icon from the app's logo: Quick Look renders the SVG, sips makes the sizes, iconutil packs them.
qlmanage -t -s 1024 -o "$TMP" "$REPO/ffscan/static/logo.svg" >/dev/null 2>&1
mkdir "$TMP/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/logo.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$TMP/logo.svg.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/icon.icns"
touch "$APP"  # make Finder pick up the icon

echo "Created $APP"
