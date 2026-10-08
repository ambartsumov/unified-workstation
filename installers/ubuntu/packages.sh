#!/usr/bin/env bash
# Recommended system packages for an Ubuntu workstation. Run with sudo.
# Installs only what is missing; replaces nothing; every source is an official repository.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

want=(git tmux fzf ripgrep fd-find jq zoxide eza btop wl-clipboard libsecret-tools syncthing rsync curl)
missing=()
for pkg in "${want[@]}"; do dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg"); done
if [ "${#missing[@]}" -gt 0 ]; then
    apt-get update -qq
    apt-get install -y --no-install-recommends "${missing[@]}"
fi

# WezTerm: the cross-platform terminal with OSC 52 clipboard support (official apt repository,
# https://wezterm.org/install/linux.html).
if ! command -v wezterm >/dev/null 2>&1; then
    keyring=/usr/share/keyrings/wezterm-fury.gpg
    tmp="$(mktemp)"
    curl -fsSL -o "$tmp" https://apt.fury.io/wez/gpg.key
    gpg --yes --dearmor -o "$keyring" "$tmp"
    rm -f "$tmp"
    chmod 644 "$keyring"
    echo "deb [signed-by=$keyring] https://apt.fury.io/wez/ * *" > /etc/apt/sources.list.d/wezterm.list
    apt-get update -qq
    apt-get install -y wezterm
fi

# Tailscale: official installer script, downloaded to a file first so it can be inspected.
if ! command -v tailscale >/dev/null 2>&1; then
    installer="$(mktemp)"
    curl -fsSL -o "$installer" https://tailscale.com/install.sh
    sh "$installer"
    rm -f "$installer"
fi

cat <<'NOTE'

Installed. Optional, by hand (not in the Ubuntu 24.04 archive):
  lazygit   https://github.com/jesseduffield/lazygit#ubuntu
  starship  https://starship.rs  (only if you set shell.prompt = "starship")
  Deskflow  flatpak install flathub org.deskflow.deskflow   (keyboard/mouse/clipboard sharing; docs/clipboard.md)
Then run: suw doctor
NOTE
