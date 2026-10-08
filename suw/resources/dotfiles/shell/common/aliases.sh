# Shared vocabulary on every workstation. `suw` stays the canonical command;
# these are conveniences and never shadow something that already exists.

_suw_alias() { command -v "$1" >/dev/null 2>&1 || alias "$1"="$2"; }
_suw_alias work    'suw mode on'
_suw_alias default 'suw mode off'
_suw_alias home    'suw home shell'
_suw_alias cloud   'suw cloud shell'
_suw_alias lg    'lazygit'
unset -f _suw_alias

# `ssh cloud` on a day when no cloud machine is rented: ask for its address and user, check its
# identity, set it up, then connect. Every other use of ssh is passed through untouched.
ssh() {
    if [ "$#" -eq 1 ] && [ "$1" = "cloud" ] && [ -t 0 ] && command -v suw >/dev/null 2>&1 \
        && ! suw cloud status --json 2>/dev/null | grep -q '"configured": *true'; then
        echo "No cloud machine is set up yet. Enter its details (Ctrl-C to cancel):"
        suw cloud add || return $?
    fi
    command ssh "$@"
}

# clip: copy stdin (or files) to the *local* clipboard — also from inside SSH and tmux.
# Over SSH it travels as an OSC 52 escape through the terminal; no X11, no clipboard daemon.
clip() {
    if [ -z "${SSH_TTY:-}" ]; then
        if command -v pbcopy >/dev/null 2>&1; then cat "$@" | pbcopy; return; fi
        if [ -n "${WAYLAND_DISPLAY:-}" ] && command -v wl-copy >/dev/null 2>&1; then cat "$@" | wl-copy; return; fi
        if command -v xclip >/dev/null 2>&1; then cat "$@" | xclip -selection clipboard; return; fi
    fi
    printf '\033]52;c;%s\a' "$(cat "$@" | base64 | tr -d '\r\n')" > /dev/tty
}

# Smarter cd, only when the tool is installed and nothing else claimed `z`.
if command -v zoxide >/dev/null 2>&1 && ! command -v z >/dev/null 2>&1; then
    if [ -n "${ZSH_VERSION:-}" ]; then eval "$(zoxide init zsh)"; else eval "$(zoxide init bash)"; fi
fi
