#!/usr/bin/env bash
# Installs VAAPI Converter for the current user — no root, nothing outside $HOME.
set -euo pipefail

APP_ID="vaapi-converter"
APP_NAME="VAAPI Converter"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

TARGET="${BIN_DIR}/${APP_ID}"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
ICON_FILE="${ICON_DIR}/${APP_ID}.svg"

uninstall() {
    rm -f "$TARGET" "$DESKTOP_FILE" "$ICON_FILE"
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "Removed ${APP_NAME}."
    exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

# --- prerequisites ---------------------------------------------------------
missing=0

if ! command -v python3 >/dev/null 2>&1; then
    echo "Missing: python3" >&2
    missing=1
elif ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Missing: tkinter. Install python3-tk (Debian/Ubuntu), python3-tkinter" >&2
    echo "         (Fedora) or tk (Arch)." >&2
    missing=1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Missing: ffmpeg. Install it, or set the path under Advanced in the app." >&2
    missing=1
elif ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q vaapi; then
    echo "Warning: your ffmpeg reports no VAAPI encoders. The app will start but" >&2
    echo "         won't be able to encode anything." >&2
fi

if [[ ! -e /dev/dri/renderD128 ]]; then
    echo "Warning: no render node at /dev/dri/renderD128. If the app finds no GPU," >&2
    echo "         add yourself to the 'render' group and log back in." >&2
fi

[[ $missing -eq 1 ]] && { echo; echo "Install the missing pieces, then run this again." >&2; exit 1; }

# A failed require makes Tcl scan its package indexes, so this works without a display.
if ! python3 -c "import sys, tkinter; t = tkinter.Tcl(); t.eval('catch {package require __probe__}'); sys.exit(not t.eval('package versions tkdnd'))" 2>/dev/null; then
    echo "Note: tkdnd isn't installed, so files can't be dropped onto the window." >&2
    echo "      It's optional; install the tkdnd package to enable it." >&2
fi

# --- install ---------------------------------------------------------------
mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"

install -m 755 "${SRC_DIR}/vaapi_converter.py" "$TARGET"

if [[ -f "${SRC_DIR}/${APP_ID}.svg" ]]; then
    install -m 644 "${SRC_DIR}/${APP_ID}.svg" "$ICON_FILE"
fi

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=${APP_NAME}
GenericName=Video Converter
Comment=Convert video on the GPU using VAAPI
Exec=${TARGET} %F
Icon=${APP_ID}
Terminal=false
Categories=AudioVideo;Video;
MimeType=video/mp4;video/x-matroska;video/webm;video/quicktime;video/x-msvideo;video/mpeg;video/x-ms-wmv;video/x-flv;
Keywords=video;convert;encode;ffmpeg;vaapi;hardware;
DESKTOP
chmod 644 "$DESKTOP_FILE"

command -v update-desktop-database >/dev/null 2>&1 &&
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 &&
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installed ${APP_NAME}."
echo "  command:  ${APP_ID}"
echo "  launcher: search for '${APP_NAME}'"
echo "  files:    right-click a video, Open With"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo
       echo "Note: ${BIN_DIR} isn't in your PATH, so the command won't work from a"
       echo "      terminal yet. Add this to ~/.bashrc or ~/.zshrc:"
       echo "        export PATH=\"\${HOME}/.local/bin:\${PATH}\""
       echo "      The launcher entry works regardless." ;;
esac
