#!/usr/bin/env bash
# Ubuntu workstation bootstrap. User-level only; system packages are opt-in (--packages).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
    echo "git is required: sudo apt install git" >&2; exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "Python 3.11+ is required (Ubuntu 24.04 ships 3.12): sudo apt install python3" >&2; exit 1
fi

if [ "${1:-}" = "--packages" ]; then
    say "Installing recommended packages (sudo)…"
    sudo bash "$ROOT/installers/ubuntu/packages.sh"
fi

exec "$ROOT/bin/suw" bootstrap ubuntu
