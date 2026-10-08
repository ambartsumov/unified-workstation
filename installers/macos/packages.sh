#!/usr/bin/env bash
# Recommended packages for a macOS workstation. Best-effort: a failed formula never aborts.
# On macOS versions Homebrew no longer supports, formulae may build from source (slow) or
# fail; the control plane does not depend on any of them.
set -uo pipefail

load_brew() {
    for b in /usr/local/bin/brew /opt/homebrew/bin/brew; do
        if [ -x "$b" ]; then eval "$("$b" shellenv)"; fi
    done
}
command -v brew >/dev/null 2>&1 || load_brew
if ! command -v brew >/dev/null 2>&1; then
    echo "Installing Homebrew (official installer, https://brew.sh; asks for your password)…"
    brew_installer="$(mktemp)"
    curl -fsSL -o "$brew_installer" https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh
    /bin/bash "$brew_installer"
    rm -f "$brew_installer"
    load_brew
fi
command -v brew >/dev/null 2>&1 || { echo "Homebrew unavailable; skipping optional packages."; exit 0; }

failed=()
for formula in tmux fzf ripgrep fd jq zoxide eza btop lazygit gh rsync starship; do
    brew list "$formula" >/dev/null 2>&1 || brew install "$formula" || failed+=("$formula")
done
for cask in wezterm hammerspoon tailscale; do
    brew list --cask "$cask" >/dev/null 2>&1 || brew install --cask "$cask" || failed+=("$cask")
done

# Deskflow (shared keyboard and mouse) is published in its own tap, not in Homebrew's
# main cask list. The version must match the other workstation's (1.27.x).
if [ ! -d /Applications/Deskflow.app ]; then
    brew tap deskflow/tap >/dev/null 2>&1 || true
    brew trust deskflow/tap >/dev/null 2>&1 || true   # newer Homebrew asks for this; older ones have no such command
    brew install --cask deskflow/tap/deskflow || failed+=("deskflow")
fi

# Syncthing (the shared work folder). On a macOS release Homebrew no longer builds for, the
# formula compiles from source and may fail: fall back to the official release binary.
if ! command -v syncthing >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/syncthing" ]; then
    if ! brew install syncthing; then
        arch="amd64"; [ "$(uname -m)" = "arm64" ] && arch="arm64"
        ver="${SUW_SYNCTHING_VERSION:-v2.1.6}"
        name="syncthing-macos-$arch-$ver"
        tmp="$(mktemp -d)"
        echo "Homebrew could not install Syncthing; fetching the official $ver release…"
        if curl -fsSL -o "$tmp/$name.zip" "https://github.com/syncthing/syncthing/releases/download/$ver/$name.zip" \
            && curl -fsSL -o "$tmp/sums" "https://github.com/syncthing/syncthing/releases/download/$ver/sha256sum.txt.asc" \
            && want="$(awk -v f="$name.zip" '$2 == f {print $1}' "$tmp/sums")" && [ -n "$want" ] \
            && [ "$(shasum -a 256 "$tmp/$name.zip" | awk '{print $1}')" = "$want" ] \
            && unzip -q "$tmp/$name.zip" -d "$tmp" && mkdir -p "$HOME/.local/bin" \
            && install -m 755 "$tmp/$name/syncthing" "$HOME/.local/bin/syncthing"; then
            echo "Syncthing $ver installed to ~/.local/bin/syncthing (checksum verified)."
        else
            failed+=("syncthing")
        fi
        rm -r "$tmp"
    fi
fi

if [ "${#failed[@]}" -gt 0 ]; then
    echo
    echo "Could not install: ${failed[*]}"
    echo "Direct downloads: wezterm.org · hammerspoon.org · tailscale.com/download · syncthing.net/downloads"
    echo "Deskflow 1.27 for this Mac: https://github.com/deskflow/deskflow/releases (the macos-x86_64 .dmg on an Intel Mac)"
    echo "The shared work folder needs Syncthing; one keyboard and mouse needs Deskflow. Then: suw bootstrap"
fi
echo "Next: open Hammerspoon once and grant Accessibility permission (menu bar + shortcut)."
