#!/bin/sh
# SUW home script: a storage replica of the shared work folder (private Syncthing instance).
# usage: sh -s -- <prepare|install|start|status|versions|stop> <folder> [release]
# The configuration itself is written by the workstation; this script never invents peers.
set -eu
action="${1:-status}"
folder="${2:-}"
case "$folder" in "~/"*) folder="$HOME/${folder#\~/}" ;; esac
case "$folder" in /*) ;; *) echo "error: the folder must be an absolute path or start with ~/" >&2; exit 2 ;; esac
sthome="$HOME/.local/state/suw/syncthing"
unit="suw-syncthing.service"
bin="$(command -v syncthing 2>/dev/null || true)"
# a copy installed for this user only (see `install`) is found even when ~/.local/bin is not on PATH
if [ -z "$bin" ] && [ -x "$HOME/.local/bin/syncthing" ]; then bin="$HOME/.local/bin/syncthing"; fi

case "$action" in
install)
    # The official release into this user's home: no administrator rights, nothing system-wide.
    if [ -n "$bin" ]; then echo "installed=yes"; echo "bin=$bin"; exit 0; fi
    ver="${3:-}"
    case "$ver" in v[0-9]*.[0-9]*.[0-9]*) ;; *) echo "error: no release given" >&2; exit 2 ;; esac
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) arch="amd64" ;;
        Linux-aarch64) arch="arm64" ;;
        *) echo "error: no official Syncthing build is known for $(uname -s) $(uname -m)" >&2; exit 5 ;;
    esac
    command -v curl >/dev/null 2>&1 || { echo "error: curl is needed on the server to download Syncthing" >&2; exit 5; }
    name="syncthing-linux-$arch-$ver"
    base="https://github.com/syncthing/syncthing/releases/download/$ver"
    tmp="$(mktemp -d)"
    trap 'rm -r "$tmp"' EXIT
    curl -fsSL --max-time 240 -o "$tmp/$name.tar.gz" "$base/$name.tar.gz" || { echo "error: could not download $name" >&2; exit 5; }
    curl -fsSL --max-time 60 -o "$tmp/sums" "$base/sha256sum.txt.asc" || { echo "error: could not download the checksums" >&2; exit 5; }
    want="$(awk -v f="$name.tar.gz" '$2 == f {print $1}' "$tmp/sums")"
    got="$(sha256sum "$tmp/$name.tar.gz" | awk '{print $1}')"
    if [ -z "$want" ] || [ "$want" != "$got" ]; then echo "error: the download does not match its published checksum; nothing was installed" >&2; exit 6; fi
    tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$tmp/$name/syncthing" "$HOME/.local/bin/syncthing"
    "$HOME/.local/bin/syncthing" --version >/dev/null 2>&1 || { rm -f "$HOME/.local/bin/syncthing"; echo "error: the downloaded Syncthing does not run on this server" >&2; exit 6; }
    echo "installed=yes"
    echo "bin=$HOME/.local/bin/syncthing"
    ;;
prepare)
    [ -n "$bin" ] || { echo "missing=syncthing"; exit 3; }
    umask 077
    mkdir -p "$sthome" "$folder/.stfolder"
    if [ ! -f "$sthome/config.xml" ]; then
        # only flags that Syncthing 1.x and 2.x both accept
        STNODEFAULTFOLDER=1 "$bin" generate --home="$sthome" >/dev/null 2>&1 || { echo "error: this Syncthing is too old (1.20 or newer is needed)" >&2; exit 4; }
    fi
    id="$("$bin" device-id --home="$sthome" 2>/dev/null || true)"                 # 2.x
    case "$id" in *-*-*-*-*-*-*-*) ;; *) id="$("$bin" --home="$sthome" --device-id 2>/dev/null || true)" ;; esac  # 1.x
    echo "id=$id"
    echo "folder=$folder"
    # the address only the tailnet can reach: the replica listens there, not on every interface
    echo "tailnet=$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    echo "---config---"
    cat "$sthome/config.xml"
    ;;
start)
    [ -n "$bin" ] || { echo "missing=syncthing"; exit 3; }
    [ -f "$sthome/config.xml" ] || { echo "error: not prepared" >&2; exit 2; }
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/$unit" <<UNIT
[Unit]
Description=Unified Workstation work folder replica (private Syncthing instance)
After=network-online.target tailscaled.service

[Service]
Type=simple
ExecStart=$bin serve --no-browser --no-restart --no-upgrade --home=$sthome
Environment=STNODEFAULTFOLDER=1
Restart=on-failure
RestartSec=10
SuccessExitStatus=3 4
RestartForceExitStatus=3 4
# the server has its own job: the copy takes what is left over
Nice=15
IOSchedulingClass=idle
CPUWeight=20
MemoryHigh=512M
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
    # without lingering the replica would stop when nobody is logged in
    loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
    echo "linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
    systemctl --user daemon-reload
    systemctl --user enable "$unit" >/dev/null 2>&1
    systemctl --user restart "$unit"
    sleep 2
    echo "active=$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    ;;
status)
    echo "installed=$([ -n "$bin" ] && echo yes || echo no)"
    echo "active=$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    echo "linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
    [ -d "$folder" ] && echo "free_kb=$(df -Pk "$folder" | awk 'NR==2 {print $4}')"
    [ -d "$folder/.stversions" ] && echo "versions=$(find "$folder/.stversions" -type f | wc -l)" || echo "versions=0"
    ;;
versions)
    [ -d "$folder/.stversions" ] || exit 0
    cd "$folder/.stversions"
    find . -type f -printf '%T@\t%s\t%P\n' | sort -rn | head -n 1000
    ;;
stop)
    systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
    echo "active=$(systemctl --user is-active "$unit" 2>/dev/null || true)"
    ;;
*)
    echo "unknown action: $action" >&2
    exit 2
    ;;
esac
