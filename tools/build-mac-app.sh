#!/bin/bash
# Builds dist/FFScan.app: ffscan in its own window, with Python, its libraries, exiftool, rclone and libusb inside,
# so the Mac it runs on needs nothing installed. Build on Apple silicon with Homebrew's exiftool, rclone and libusb.
# Not signed with a developer ID: on another Mac, allow it once in System Settings → Privacy & Security → Open Anyway.
set -euo pipefail
cd "$(dirname "$0")/.."
B=build/mac
rm -rf "$B" dist/FFScan.app
mkdir -p "$B/bin"

[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt -r requirements-desktop.txt
if [ ! -x tools/orient ] || [ tools/orient.swift -nt tools/orient ]; then
  swiftc -O tools/orient.swift -o tools/orient
fi

# Helpers found on PATH (desktop.py puts bin/ first). exiftool is pure Perl: run it with macOS's own perl.
cp "$(brew --prefix rclone)/bin/rclone" "$B/bin/"
EX="$(brew --prefix exiftool)/libexec"
cp "$EX/bin/exiftool" "$B/bin/exiftool.pl"
cp -R "$EX/lib/perl5" "$B/bin/exiftool-lib"
cat > "$B/bin/exiftool" <<'EOF'
#!/bin/sh
here="$(dirname "$0")"
exec /usr/bin/perl -I"$here/exiftool-lib" "$here/exiftool.pl" "$@"
EOF
chmod +x "$B/bin/exiftool"
# pyusb looks for this name (ctypes find_library, which searches DYLD_LIBRARY_PATH: set by desktop.py).
cp "$(brew --prefix libusb)/lib/libusb-1.0.dylib" "$B/"

# Icon from the logo: Quick Look renders the SVG, sips makes the sizes, iconutil packs them.
qlmanage -t -s 1024 -o "$B" ffscan/static/logo.svg >/dev/null 2>&1
mkdir "$B/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$B/logo.svg.png" --out "$B/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$B/logo.svg.png" --out "$B/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$B/icon.iconset" -o "$B/icon.icns"

.venv/bin/pyinstaller --noconfirm --log-level WARN --windowed --name FFScan \
  --osx-bundle-identifier io.github.ffscan --icon "$PWD/$B/icon.icns" \
  --distpath dist --workpath "$B/work" --specpath "$B" --paths . \
  --add-data "$PWD/ffscan/static:ffscan/static" \
  --add-binary "$PWD/tools/orient:tools" \
  --add-data "$PWD/$B/bin:bin" \
  --add-binary "$PWD/$B/libusb-1.0.dylib:." \
  --collect-submodules uvicorn \
  ffscan/desktop.py

echo "Built dist/FFScan.app"
