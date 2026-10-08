#!/bin/sh
# SUW cloud script: join the tailnet. Idempotent.
# usage: sh -s -- <hostname>     (auth key on stdin, never on the command line)
set -eu
name="$1"
case "$name" in *[!a-z0-9-]*|"") echo "invalid hostname" >&2; exit 2 ;; esac
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo -n"

umask 077
keyfile=$(mktemp)
trap 'rm -f "$keyfile"' EXIT INT TERM
cat > "$keyfile"
[ -s "$keyfile" ] || { echo "no auth key supplied" >&2; exit 2; }

if ! command -v tailscale >/dev/null 2>&1; then
    # Official installer: https://tailscale.com/docs/how-to/set-up-servers
    curl -fsSL https://tailscale.com/install.sh | $SUDO sh >/dev/null
fi
$SUDO systemctl enable --now tailscaled >/dev/null 2>&1 || true
if [ -n "$SUDO" ]; then $SUDO chown root "$keyfile" 2>/dev/null || true; fi
$SUDO tailscale up --auth-key="file:$keyfile" --hostname="$name" --accept-dns=true --timeout=60s
printf 'tailscale_ip=%s\n' "$($SUDO tailscale ip -4 2>/dev/null | head -n1)"
printf 'tailscale_name=%s\n' "$($SUDO tailscale status --json --peers=false 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -n1 | sed 's/\.$//')"
