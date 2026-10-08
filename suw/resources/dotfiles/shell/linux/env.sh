# Linux-specific environment.
# Debian/Ubuntu ship fd as `fdfind`.
if ! command -v fd >/dev/null 2>&1 && command -v fdfind >/dev/null 2>&1; then
    alias fd='fdfind'
fi
