# macOS-specific environment.
# Homebrew (Intel prefix first: this fleet's Mac is Intel; Apple Silicon also handled).
for _suw_brew in /usr/local/bin/brew /opt/homebrew/bin/brew; do
    if [ -x "$_suw_brew" ] && ! command -v brew >/dev/null 2>&1; then
        eval "$("$_suw_brew" shellenv)"
    fi
done
unset _suw_brew
# The Tailscale app bundles its CLI here.
if ! command -v tailscale >/dev/null 2>&1 && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
    alias tailscale='/Applications/Tailscale.app/Contents/MacOS/Tailscale'
fi
