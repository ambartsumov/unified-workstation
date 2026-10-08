#!/bin/sh
# SUW cloud script: read-only data-loss report before a node is given up.
# usage: sh -s -- [output-path ...]
# Emits key=value lines: dirty=, unpushed=, noremote=, job=, mount=, output=
has() { command -v "$1" >/dev/null 2>&1; }

for root in "$HOME" /workspace /root /data /mnt; do
    [ -d "$root" ] || continue
    find "$root" -maxdepth 5 -name .git -type d -not -path '*/node_modules/*' -not -path '*/.cache/*' 2>/dev/null
done | sort -u | while IFS= read -r gitdir; do
    repo=$(dirname "$gitdir")
    n=$(git -C "$repo" status --porcelain 2>/dev/null | grep -c .)
    [ "$n" -gt 0 ] && printf 'dirty=%s|%s\n' "$repo" "$n"
    if [ -z "$(git -C "$repo" remote 2>/dev/null)" ]; then
        printf 'noremote=%s\n' "$repo"
        continue
    fi
    u=$(git -C "$repo" log --branches --not --remotes --oneline 2>/dev/null | grep -c .)
    [ "$u" -gt 0 ] && printf 'unpushed=%s|%s\n' "$repo" "$u"
    t=$(git -C "$repo" stash list 2>/dev/null | grep -c .)
    [ "$t" -gt 0 ] && printf 'dirty=%s|%s stash\n' "$repo" "$t"
done

if has tmux; then tmux list-sessions -F 'job=tmux session #{session_name} (#{session_windows} windows)' 2>/dev/null; fi
if has screen; then screen -ls 2>/dev/null | awk '/\t/ { print "job=screen " $1 }'; fi
if has docker; then docker ps --format 'job=container {{.Names}} ({{.Image}})' 2>/dev/null; fi
if has nvidia-smi; then
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null |
        awk -F', ' 'NF { print "job=gpu process " $2 " (pid " $1 ", " $3 ")" }'
fi
df -PT 2>/dev/null | awk 'NR > 1 && $2 !~ /tmpfs|devtmpfs|overlay|squashfs|efivarfs|vfat/ && $7 != "/" && $7 !~ /^\/(boot|snap|run|dev|sys|proc)/ { print "mount=" $7 " (" $2 ", " $6 " used)" }'

for out in "$@"; do
    case "$out" in "~/"*) out="$HOME/${out#\~/}" ;; esac
    [ -e "$out" ] || continue
    count=$(find "$out" -type f 2>/dev/null | head -n 100000 | grep -c .)
    [ "$count" -gt 0 ] && printf 'output=%s|%s files, %s\n' "$out" "$count" "$(du -sh "$out" 2>/dev/null | cut -f1)"
done
exit 0
