#!/bin/sh
# SUW cloud script: capability-based bootstrap. Idempotent; installs only what a profile needs.
# usage: sh -s -- <minimal|compute|cuda|inference|training> [docker]
set -eu
profile="${1:-minimal}"
want_docker="${2:-}"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo -n"
log() { printf 'step=%s\n' "$*"; }

command -v apt-get >/dev/null 2>&1 || { echo "unsupported: only apt-based images are handled" >&2; exit 3; }
export DEBIAN_FRONTEND=noninteractive
pkgs="git tmux curl ca-certificates"
case "$profile" in
    minimal) ;;
    compute|cuda|inference|training) pkgs="$pkgs btop python3 python3-venv build-essential rsync jq" ;;
    *) echo "unknown profile: $profile" >&2; exit 2 ;;
esac

missing=""
for p in $pkgs; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
    $SUDO apt-get update -qq
    # shellcheck disable=SC2086
    $SUDO apt-get install -y -qq --no-install-recommends $missing >/dev/null
    log "installed:$missing"
else
    log "packages already present"
fi

if [ "$profile" != "minimal" ] && ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 && log "installed: uv"
fi

if ! command -v gh >/dev/null 2>&1 && [ "$profile" != "minimal" ]; then
    $SUDO apt-get install -y -qq gh >/dev/null 2>&1 && log "installed: gh" || log "gh not available from apt (skipped)"
fi

case "$profile" in
    cuda|inference|training)
        if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
            log "gpu ok: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
        else
            log "WARNING no working NVIDIA driver; choose a GPU image from the provider (drivers are not installed automatically)"
        fi ;;
esac

if [ "$want_docker" = "docker" ] && ! command -v docker >/dev/null 2>&1; then
    $SUDO apt-get install -y -qq docker.io >/dev/null && log "installed: docker.io"
fi

# Remote clipboard through tmux/OSC 52 (interactive sessions only; no clipboard daemon).
conf="$HOME/.tmux.conf"
if ! grep -q 'suw managed' "$conf" 2>/dev/null; then
    cat >> "$conf" <<'EOF'
# >>> suw managed block >>>
set -g set-clipboard on
set -g allow-passthrough on
set -g mouse on
set -g history-limit 50000
set -ga terminal-overrides ',*:Ms=\E]52;c;%p2%s\7'
# <<< suw managed block <<<
EOF
    log "tmux clipboard (OSC 52) configured"
fi
# Versioned, append-only record of what was applied (resumable: every step above checks first).
state="$HOME/.local/state/suw"
mkdir -p "$state"
printf '%s profile=%s version=2\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$profile" >> "$state/bootstrap.log"
printf '2\n' > "$state/bootstrap.version"
log "profile $profile complete"
