#!/usr/bin/env bash
# macOS workstation bootstrap (Intel and Apple Silicon). Safe to re-run.
#
# Designed for an older Intel MacBook: the control plane needs only git and one Python,
# so it works even where Homebrew no longer ships prebuilt bottles. Everything Homebrew
# installs is optional comfort and is attempted best-effort.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

# 1. git (Apple command line tools)
if ! xcode-select -p >/dev/null 2>&1; then
    say "Apple command line tools are required (provides git)."
    xcode-select --install || true
    echo "A system dialog opened. When it finishes, run this installer again."
    exit 1
fi

# 2. Python 3.11+ — uv downloads a standalone build; no compiler, no Homebrew needed.
PIN="$HOME/.config/suw/python"
mkdir -p "$HOME/.config/suw" && chmod 700 "$HOME/.config/suw"
find_python() {
    local pinned=""
    [ -r "$PIN" ] && read -r pinned < "$PIN"
    for c in "$pinned" python3.13 python3.12 python3.11 python3; do
        [ -n "$c" ] || continue
        p="$(command -v "$c" 2>/dev/null || true)"; [ -n "$p" ] || continue
        if "$p" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then echo "$p"; return 0; fi
    done
    return 1
}
if ! PY="$(find_python)"; then
    say "Installing a private Python 3.12 (via uv)…"
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        # Official uv installer (https://docs.astral.sh/uv/), saved to a file before running.
        uv_installer="$(mktemp)"
        curl -LsSf -o "$uv_installer" https://astral.sh/uv/install.sh
        sh "$uv_installer"
        rm -f "$uv_installer"
    fi
    uv python install 3.12
    PY="$(uv python find 3.12)"
fi
echo "$PY" > "$PIN"
note "Python: $PY"

# 3. Optional comfort packages
if [ "${1:-}" = "--packages" ]; then
    bash "$ROOT/installers/macos/packages.sh" || note "some optional packages failed; continuing"
fi

# 4. Tailscale presence check (GUI app: needs a human login once)
if ! command -v tailscale >/dev/null 2>&1 && [ ! -d /Applications/Tailscale.app ]; then
    say "Tailscale is not installed yet."
    note "Install it from https://tailscale.com/download/mac (or the App Store), sign in,"
    note "then re-run this installer so the Mac registers itself in the inventory."
fi

# 5. The same idempotent bootstrap as on Ubuntu
exec "$ROOT/bin/suw" bootstrap macos
