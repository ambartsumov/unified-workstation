#!/bin/sh
# Acceptance run for a machine that could not be tested when SUW was written.
#
#   scripts/accept.sh mac      on the MacBook, after docs/mac-day-one.md
#   scripts/accept.sh cloud    on a workstation, after `suw cloud add`
#   scripts/accept.sh home     on a workstation: the home server and its copy of the work folder
#
# Every line is one check: PASS, FAIL, or MANUAL (needs eyes and hands; the instruction is
# printed). Nothing here changes the machine except two throw-away files it creates and removes
# inside the work folder / the cloud project area. Exit code: 0 when nothing FAILED.
# A PASS printed here is the only thing that may be called "verified" for that item.
set -u
what="${1:-}"
pass=0
fail=0
manual=0

say() { printf '%-7s %s\n' "$1" "$2"; }
ok() { pass=$((pass + 1)); say "PASS" "$1"; }
bad() { fail=$((fail + 1)); say "FAIL" "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
todo() { manual=$((manual + 1)); say "MANUAL" "$1"; printf '        %s\n' "$2"; }

# check "<title>" <command…>: PASS when the command exits 0
check() {
    title="$1"
    shift
    if out="$("$@" 2>&1)"; then ok "$title"; else bad "$title" "$(printf '%s' "$out" | tail -n 1)"; fi
}

# json_is "<title>" "<key>" "<value>" <command…>: PASS when the JSON output has "key": "value"
json_is() {
    title="$1"
    key="$2"
    want="$3"
    shift 3
    out="$("$@" 2>/dev/null)"
    if printf '%s' "$out" | grep -Eq "\"$key\": *\"?$want\"?[,}]?[[:space:]]*\$"; then ok "$title"; else bad "$title" "expected $key = $want"; fi
}

wait_for() { # wait_for <seconds> <command…>
    left="$1"
    shift
    while [ "$left" -gt 0 ]; do
        "$@" >/dev/null 2>&1 && return 0
        sleep 5
        left=$((left - 5))
    done
    return 1
}

command -v suw >/dev/null 2>&1 || { echo "suw is not on PATH: run ./install.sh first"; exit 2; }
work="$(suw work status --json 2>/dev/null | sed -n 's/^ *"path": *"\(.*\)",*$/\1/p' | head -n 1)"

case "$what" in
mac)
    [ "$(uname -s)" = "Darwin" ] || { echo "This run is for the MacBook itself. On this machine nothing about macOS can be proven."; exit 2; }
    echo "MacBook acceptance — $(sw_vers -productVersion) $(uname -m) — $(suw --version 2>/dev/null)"
    check "1  suw doctor has no failure" suw doctor
    check "2  background daemon runs" suw daemon status
    check "3  daemon is a LaunchAgent that starts at login" test -f "$HOME/Library/LaunchAgents/io.github.ambartsumov.suwd.plist"
    check "4  Tailscale connected" sh -c 'tailscale status >/dev/null 2>&1 || /Applications/Tailscale.app/Contents/MacOS/Tailscale status >/dev/null'
    check "5  ssh home answers" ssh -o BatchMode=yes home true
    check "6  ssh ubuntu answers" ssh -o BatchMode=yes ubuntu true
    json_is "7  home server ONLINE" status ONLINE suw home status --json
    check "8  WezTerm installed" sh -c 'command -v wezterm >/dev/null || test -d /Applications/WezTerm.app'
    check "9  tmux installed" tmux -V
    check "10 Syncthing installed" sh -c 'command -v syncthing >/dev/null || test -x "$HOME/.local/bin/syncthing"'
    json_is "11 work folder service running" running true suw work status --json
    if wait_for 300 sh -c 'suw work status --json | grep -q "\"state\": *\"IN_SYNC\""'; then ok "12 work folder IN_SYNC with the other workstation"; else bad "12 work folder IN_SYNC with the other workstation" "suw work status; pair first: suw work pair ubuntu (and suw work pair mac on Ubuntu)"; fi
    if [ -n "$work" ] && [ -d "$work" ]; then
        probe="$work/.suw-accept-mac-$$"
        mkdir -p "$probe/.скрытая" && printf 'строка — ñ 日本語\n' > "$probe/.скрытая/.env.sample"
        if wait_for 180 ssh -o BatchMode=yes ubuntu "test -f \"\$HOME/Desktop/work/.suw-accept-mac-$$/.скрытая/.env.sample\""; then ok "13 a hidden folder with a Cyrillic name reaches Ubuntu"; else bad "13 a hidden folder with a Cyrillic name reaches Ubuntu" "suw work status on both machines"; fi
        rm -r "$probe"
        if wait_for 180 ssh -o BatchMode=yes ubuntu "test ! -e \"\$HOME/Desktop/work/.suw-accept-mac-$$\""; then ok "14 its deletion reaches Ubuntu"; else bad "14 its deletion reaches Ubuntu"; fi
    else
        bad "13 work folder exists" "suw work setup"
    fi
    check "15 Claude Code notes live in the work folder" sh -c 'suw work claude | grep -q "● work .*shared"'
    check "16 clipboard over SSH (5 runs)" suw clipboard test --via home --window --repeat 5
    check "17 clipboard local (5 runs)" suw clipboard test --window --repeat 5
    json_is "18 keyboard/mouse sharing CONNECTED" state CONNECTED suw peripherals status --json
    check "19 Hammerspoon installed" test -d /Applications/Hammerspoon.app
    check "20 Workstation Mode on, twice: no duplicate windows" sh -c 'suw mode on && suw mode on && [ "$(pgrep -x wezterm-gui | wc -l)" -le 2 ]'
    check "21 Workstation Mode off" suw mode off --close-all
    check "22 rollback lists only SUW-owned changes" suw rollback --dry-run
    check "23 the whole test suite passes on macOS" sh -c 'cd "$(dirname "$(readlink "$(command -v suw)" 2>/dev/null || command -v suw)")/.." && python3 -m pytest tests -q'
    todo "24 one keyboard and mouse" "Move the pointer off the edge of the Ubuntu screen onto the Mac and back; type on both."
    todo "25 shared clipboard" "Copy text on Ubuntu, paste on the Mac; then the other way round."
    todo "26 shortcut and leave dialog" "Press Ctrl+Opt+Cmd+D twice. The second press asks Save & Close / Cancel; try both."
    todo "27 sleep and wake" "Close the lid for 10 minutes, open it, wait a minute, run: scripts/accept.sh mac"
    todo "28 restart" "Restart the Mac, log in, wait a minute, run: scripts/accept.sh mac"
    todo "29 offline catch-up" "Turn Wi-Fi off, edit a file in the work folder on both machines, turn it on: both edits arrive; the same file edited twice becomes a conflict copy (suw work conflicts)."
    ;;
cloud)
    echo "Cloud acceptance — $(suw --version 2>/dev/null)"
    if ! suw cloud status --json 2>/dev/null | grep -q '"configured": *true'; then
        echo "No cloud machine is configured, so there is nothing to accept yet. First: suw cloud add <address> --user <name>"
        exit 2
    fi
    ok "1  a cloud machine is configured"
    check "2  its identity matches the pinned fingerprint" suw cloud verify
    check "3  ssh cloud answers, host key checked" ssh -o BatchMode=yes -o StrictHostKeyChecking=yes cloud true
    json_is "4  cloud status ONLINE" status ONLINE suw cloud status --json
    check "5  tmux on the server" ssh -o BatchMode=yes cloud tmux -V
    check "6  clipboard from the server (5 runs)" suw clipboard test --via cloud --window --repeat 5
    demo="$(mktemp -d)/suw-accept-cloud"
    mkdir -p "$demo/.hidden" && printf 'hello — привет\n' > "$demo/readme.txt" && printf 'x\n' > "$demo/.hidden/note"
    if [ -n "$work" ] && [ -d "$work" ]; then
        rm -rf "$work/suw-accept-cloud" && cp -R "$demo" "$work/suw-accept-cloud"
        check "7  one project is sent (and only that one)" suw cloud project push suw-accept-cloud --yes
        check "8  the server holds exactly that project" sh -c 'ssh -o BatchMode=yes cloud "ls -A suw-projects" | grep -qx suw-accept-cloud'
        ssh -o BatchMode=yes cloud 'echo result > suw-projects/suw-accept-cloud/result.txt' >/dev/null 2>&1
        check "9  results come back" sh -c "suw cloud project pull suw-accept-cloud --yes && test -f '$work/suw-accept-cloud/result.txt'"
        check "10 the cloud copy can be removed" suw cloud project remove suw-accept-cloud --yes
        rm -rf "$work/suw-accept-cloud"
    else
        bad "7  work folder exists" "suw work setup"
    fi
    before="$(suw cloud status --json 2>/dev/null | sed -n 's/^ *"label": *"\(.*\)",*$/\1/p')"
    if suw cloud replace 192.0.2.1 --yes >/dev/null 2>&1; then bad "11 a replacement that cannot be reached is refused"; else
        after="$(suw cloud status --json 2>/dev/null | sed -n 's/^ *"label": *"\(.*\)",*$/\1/p')"
        if [ "$before" = "$after" ] && ssh -o BatchMode=yes cloud true 2>/dev/null; then ok "11 a failed replacement leaves the current cloud active"; else bad "11 a failed replacement leaves the current cloud active" "was '$before', now '$after'"; fi
    fi
    todo "12 replacement by a real second machine" "suw cloud replace <new address>: ssh cloud must land on the new machine without editing anything."
    todo "13 work survives the session" "On the server start something in tmux, close the laptop lid, reopen: ssh cloud, tmux attach."
    ;;
home)
    echo "Home acceptance — $(suw --version 2>/dev/null)"
    json_is "1  home server ONLINE" status ONLINE suw home status --json
    check "2  every step to the server passes" suw home doctor
    check "3  ssh home answers, host key checked" ssh -o BatchMode=yes -o StrictHostKeyChecking=yes home true
    json_is "4  the copy of the work folder is ONLINE" state ONLINE suw home replica status --json
    check "5  the copy listens on the tailnet address only" sh -c 'ssh -o BatchMode=yes home "ss -ltn" | grep ":22000 " | grep -vq "100\\." && exit 1 || exit 0'
    check "6  the copy survives a logout (linger)" sh -c 'ssh -o BatchMode=yes home "loginctl show-user \$(id -un) -p Linger --value" | grep -qx yes'
    if [ -n "$work" ] && [ -d "$work" ]; then
        name=".suw-accept-home-$$.txt"
        printf 'первая версия\n' > "$work/$name"
        if wait_for 600 ssh -o BatchMode=yes home "test -f \"\$HOME/work/$name\""; then ok "7  a new hidden file reaches the server"; else bad "7  a new hidden file reaches the server" "the first copy may still be running: suw work status"; fi
        rm -f "$work/$name"
        if wait_for 300 sh -c "suw work versions --home 2>/dev/null | grep -q 'suw-accept-home-$$'"; then ok "8  after deleting it here, the server still has the version"; else bad "8  after deleting it here, the server still has the version" "suw work versions --home"; fi
    else
        bad "7  work folder exists" "suw work setup"
    fi
    check "9  clipboard from the server (5 runs)" suw clipboard test --via home --window --repeat 5
    ;;
*)
    echo "usage: scripts/accept.sh mac|cloud|home"
    exit 2
    ;;
esac

echo
echo "$pass passed, $fail failed, $manual to do by hand"
[ "$fail" -eq 0 ]
