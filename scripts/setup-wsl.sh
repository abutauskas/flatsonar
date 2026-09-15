#!/usr/bin/env bash
# One-time setup for running the Flatsea client inside WSL2 (Ubuntu) or any Debian-based distro.
set -euo pipefail

if ! command -v apt-get >/dev/null; then
  echo "This script targets Debian/Ubuntu. Install the equivalents manually:" >&2
  echo "  python3-gi gtk4 libadwaita flatpak flatpak-builder ostree clamav" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  python3 python3-pip python3-venv \
  python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  flatpak gir1.2-flatpak-1.0 flatpak-builder ostree \
  clamav clamav-freshclam \
  libadwaita-1-0 gnome-icon-theme adwaita-icon-theme

# Flathub remote for the current user.
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

# ClamAV signatures. freshclam may already be running as a service; ignore lock errors.
sudo freshclam || true

# Sanity check: can bubblewrap create a sandbox? If this fails on WSL2, enable systemd
# in /etc/wsl.conf ([boot] systemd=true), run `wsl --shutdown` from Windows, and retry.
if command -v bwrap >/dev/null; then
  if bwrap --ro-bind / / --dev /dev --proc /proc true 2>/dev/null; then
    echo "bubblewrap OK"
  else
    echo "WARNING: bubblewrap could not create a sandbox. See comment above." >&2
  fi
fi

echo
echo "Done. Next:"
echo "  pip install --user -e core -e client"
echo "  FLATSEA_API=http://localhost:8000 python3 -m flatsea"
