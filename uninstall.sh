#!/usr/bin/env bash
# Removes the menu entry, icon and login item. Saved settings in
# ~/.config/headroom are left alone.
set -euo pipefail
rm -f "$HOME/.local/share/applications/headroom.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/headroom.svg" \
      "$HOME/.config/autostart/headroom.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "Headroom removed from the applications menu."
