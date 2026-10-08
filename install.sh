#!/usr/bin/env bash
# Unified Workstation installer. Safe to re-run: every step is idempotent and journaled.
#
#   ./install.sh               set this machine up (no root needed)
#   ./install.sh --packages    also install recommended system packages (asks for sudo / uses brew)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$(uname -s)" in
    Darwin) exec bash "$ROOT/installers/macos/bootstrap.sh" "$@" ;;
    Linux)  exec bash "$ROOT/installers/ubuntu/bootstrap.sh" "$@" ;;
    *) echo "Unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac
