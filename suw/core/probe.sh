#!/bin/sh
# SUW probe v1 — read-only hardware/health report, key=value on stdout.
# Runs locally and over SSH (`ssh host sh -s -- unit1 unit2 < probe.sh`).
# Arguments: service names to report (systemd units or docker container names).
# POSIX sh; Linux and macOS. Makes no changes to the machine.

kv() { printf '%s=%s\n' "$1" "$2"; }
has() { command -v "$1" >/dev/null 2>&1; }

kv probe 1
kv hostname "$(hostname 2>/dev/null)"
kv kernel "$(uname -sr 2>/dev/null)"
kv arch "$(uname -m 2>/dev/null)"
sys=$(uname -s 2>/dev/null)

if [ "$sys" = "Darwin" ]; then
    kv os "macOS $(sw_vers -productVersion 2>/dev/null)"
    kv cpu_model "$(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
    cores=$(sysctl -n hw.logicalcpu 2>/dev/null); kv cores "$cores"
    mem=$(sysctl -n hw.memsize 2>/dev/null); kv ram_mb "$(( ${mem:-0} / 1048576 ))"
    vm_stat 2>/dev/null | awk -v total="${mem:-0}" '
        /page size of/ { ps=$8 }
        /Pages active/ { a=$3 } /Pages wired/ { w=$4 } /occupied by compressor/ { c=$5 }
        END { if (total > 0) printf "ram_used_pct=%d\n", (a + w + c) * ps * 100 / total }'
    ps -A -o %cpu 2>/dev/null | awk -v n="${cores:-1}" 'NR > 1 { s += $1 } END { v = s / n; if (v > 100) v = 100; printf "cpu_pct=%d\n", v }'
    kv load1 "$(sysctl -n vm.loadavg 2>/dev/null | awk '{ print $2 }')"
    pmset -g batt 2>/dev/null | awk 'match($0, /[0-9]+%/) { printf "battery_pct=%s\n", substr($0, RSTART, RLENGTH - 1) }
        /AC Power/ { print "battery_state=charging" } /Battery Power/ { print "battery_state=discharging" }'
    df -k / 2>/dev/null | awk 'NR == 2 { printf "disk_total_gb=%d\ndisk_free_gb=%d\ndisk_used_pct=%d\n", $2 / 1048576, $4 / 1048576, ($2 - $4) * 100 / $2 }'
else
    if [ -r /etc/os-release ]; then
        kv os "$(. /etc/os-release; printf '%s' "${PRETTY_NAME:-$NAME}")"
    else
        kv os "$sys"
    fi
    kv cpu_model "$(awk -F': ' '/model name/ { print $2; exit }' /proc/cpuinfo 2>/dev/null)"
    kv cores "$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
    awk '/MemTotal/ { t=$2 } /MemAvailable/ { a=$2 }
        END { if (t > 0) printf "ram_mb=%d\nram_used_pct=%d\n", t / 1024, (t - a) * 100 / t }' /proc/meminfo 2>/dev/null
    cpu_sample() { awk '/^cpu / { print $2 + $3 + $4 + $6 + $7 + $8, $5 + $6; exit }' /proc/stat 2>/dev/null; }
    s1=$(cpu_sample); sleep 0.3 2>/dev/null || sleep 1; s2=$(cpu_sample)
    printf '%s %s\n' "$s1" "$s2" | awk '{ b = $3 - $1; i = $4 - $2; if (b + i > 0) printf "cpu_pct=%d\n", b * 100 / (b + i) }'
    kv load1 "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
    for bat in /sys/class/power_supply/BAT*; do
        [ -r "$bat/capacity" ] || continue
        kv battery_pct "$(cat "$bat/capacity")"
        kv battery_state "$(tr '[:upper:]' '[:lower:]' < "$bat/status" 2>/dev/null)"
        break
    done
    df -Pk / 2>/dev/null | awk 'NR == 2 { printf "disk_total_gb=%d\ndisk_free_gb=%d\ndisk_used_pct=%d\n", $2 / 1048576, $4 / 1048576, ($2 - $4) * 100 / $2 }'
fi

if has nvidia-smi; then
    nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null |
        while IFS= read -r line; do kv gpu "$line"; done
    kv cuda "$(nvidia-smi 2>/dev/null | awk 'match($0, /CUDA Version: [0-9.]+/) { print substr($0, RSTART + 14, RLENGTH - 14); exit }')"
    kv gpu_procs "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)"
fi

if has docker; then kv docker "$(docker --version 2>/dev/null | awk '{ print $3 }' | tr -d ,)"; fi
if has tailscale; then
    kv tailscale "$(tailscale status --peers=false 2>/dev/null | awk 'NR == 1 { print $2 }')"
fi
if has suw-agent; then kv agent "$(suw-agent --version 2>/dev/null)"; fi
if [ -r /proc/uptime ]; then kv uptime_s "$(cut -d. -f1 /proc/uptime)"; fi

for unit in "$@"; do
    case "$unit" in *[!A-Za-z0-9_.@-]*) continue ;; esac
    st=""
    if has systemctl; then
        st=$(systemctl is-active "$unit" 2>/dev/null)
        if [ "$st" != "active" ]; then
            ust=$(systemctl --user is-active "$unit" 2>/dev/null)
            [ "$ust" = "active" ] && st=active
        fi
    fi
    if [ "$st" != "active" ] && has docker; then
        dst=$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' "$unit" 2>/dev/null)
        case "$dst" in running|running/healthy|running/starting) st=active ;; "") ;; *) st="$dst" ;; esac
    fi
    kv "service.$unit" "${st:-unknown}"
done
exit 0
