#!/bin/bash
# Builds two apps:
#  ~/.snip2tex/Snip2TeX Engine.app  the menu-bar app that runs snip2tex from this
#                                    folder (server, built-in model, dashboard)
#  /Applications/Snip2TeX.app        a compiled AppleScript launcher, which is what
#                                    shows up in Apps/Launchpad and Spotlight: it
#                                    starts the engine (which opens its window),
#                                    brings the window back if it's running, or
#                                    restarts it if the code changed since
# Re-run after moving this folder.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.snip2tex/Snip2TeX Engine.app"
if [ -w /Applications ]; then LAUNCHER="/Applications/Snip2TeX.app"; else LAUNCHER="$HOME/Applications/Snip2TeX.app"; fi
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

# older builds put the engine itself in an Applications folder
for old in "$HOME/Applications/Snip2TeX.app" "/Applications/Snip2TeX.app"; do
    if [ -d "$old" ]; then
        "$LSREGISTER" -u "$old" 2>/dev/null || true
        rm -rf "$old"
    fi
done
mkdir -p "$HOME/.snip2tex"

command -v uv >/dev/null || { echo "needs uv: https://docs.astral.sh/uv/"; exit 1; }
(cd "$HERE" && uv sync -q)

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# the executable embeds Python (launcher.c), so the running process is the
# bundle's own signed binary; macOS 26 hides menu bar items otherwise
PY="$HERE/.venv/bin/python"
PYBASE="$("$PY" -c 'import sys; print(sys.base_prefix)')"
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
clang -O2 -o "$APP/Contents/MacOS/Snip2TeX" "$HERE/launcher.c" \
    -I"$PYBASE/include/python$PYVER" -L"$PYBASE/lib" -lpython"$PYVER" -Wl,-rpath,"$PYBASE/lib" \
    -DPROJECT_DIR="\"$HERE\"" -DVENV_PYTHON="\"$PY\"" -DLOG_FILE="\"$HOME/.snip2tex/app.log\""

cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Snip2TeX</string>
  <key>CFBundleDisplayName</key><string>Snip2TeX</string>
  <key>CFBundleIdentifier</key><string>local.snip2tex.engine</string>
  <key>CFBundleExecutable</key><string>Snip2TeX</string>
  <key>CFBundleIconFile</key><string>Snip2TeX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PL

# icon: the tablet's ∑ glyph on a white rounded square
"$HERE/.venv/bin/python" - "$APP/Contents/Resources" <<'PY'
import subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw
out = Path(sys.argv[1])
sigma = [(10, 6), (38, 6), (38, 14), (35, 14), (35, 9), (15.6, 9), (28.1, 24), (15.6, 39), (35, 39),
         (35, 34), (38, 34), (38, 42), (10, 42), (10, 40.4), (24.2, 24), (10, 7.6)]
def icon(n):
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = n * 0.09
    d.rounded_rectangle((m, m, n - m, n - m), radius=n * 0.2, fill="white", outline=(200, 200, 200), width=max(1, n // 128))
    k, off = n * 0.62 / 48, n * 0.19
    d.polygon([(off + x * k, off + y * k) for x, y in sigma], fill="black")
    return im
with tempfile.TemporaryDirectory() as t:
    s = Path(t) / "Snip2TeX.iconset"
    s.mkdir()
    for n in (16, 32, 128, 256, 512):
        icon(n).save(s / f"icon_{n}x{n}.png")
        icon(n * 2).save(s / f"icon_{n}x{n}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(s), "-o", str(out / "Snip2TeX.icns")], check=True)
PY

codesign --force --deep --sign - "$APP" 2>/dev/null || true

# the launcher: a real applet, so macOS lists it like any other app
osacompile -o "$LAUNCHER" - <<OSA
set engine to POSIX file "$APP"
set serverUp to (do shell script "curl -s -m 1 http://localhost:8799/ping >/dev/null && echo yes || echo no")
if serverUp is "yes" then
    set stale to (do shell script "curl -s -m 2 http://localhost:8799/api/stale || true")
    if stale is "yes" then
        -- running older code than this folder has: restart it
        do shell script "curl -s -m 2 -X POST http://localhost:8799/api/quit; sleep 2"
        do shell script "open -g " & quoted form of POSIX path of engine
    else
        do shell script "curl -s -m 2 -X POST http://localhost:8799/api/show"
    end if
else
    do shell script "open -g " & quoted form of POSIX path of engine
end if
OSA
cp "$APP/Contents/Resources/Snip2TeX.icns" "$LAUNCHER/Contents/Resources/applet.icns"
# osacompile also writes an asset catalog whose icon wins over applet.icns
rm -f "$LAUNCHER/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$LAUNCHER/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier local.snip2tex" "$LAUNCHER/Contents/Info.plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.snip2tex" "$LAUNCHER/Contents/Info.plist"
codesign --force --deep --sign - "$LAUNCHER" 2>/dev/null || true
touch "$LAUNCHER"
"$LSREGISTER" -f "$LAUNCHER"

echo "built $LAUNCHER (engine: $APP)"
