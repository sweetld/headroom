#!/usr/bin/env bash
# Adds Headroom to the applications menu. "Start at login" is switched on and
# off from the widget's own right-click menu, not here.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$APPS" "$ICONS"
install -m 644 "$DIR/icon.svg" "$ICONS/headroom.svg"
cat > "$APPS/headroom.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Headroom
GenericName=Memory and Swap Monitor
Comment=Shows whether swap is spilling onto disk, before it freezes the desktop
Exec=/usr/bin/python3 $DIR/headroom.py
Icon=headroom
Terminal=false
Categories=System;Monitor;
Keywords=swap;memory;zram;ram;monitor;freeze;
StartupNotify=false
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "Headroom is in the applications menu."
