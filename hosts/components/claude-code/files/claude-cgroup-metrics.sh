#!/usr/bin/env bash
# Cgroup resource metrics for claude-rc.service, user-1000.slice and each live fan-out batch
# unit. Shipped by the homelab host package (hosts/components/claude-code); edits are overwritten.
#
# claude-rc.service is bounded (MemoryHigh/MemorySwapMax) and user-1000.slice (sessions started
# with `claude agents` from an SSH shell) is not, so this exports the counters of both cgroups.
# This host sets DefaultMemoryAccounting/CPUAccounting=yes, so the counters are already
# populated on cgroupfs and free to read. Written as a node-exporter textfile gauge set so no
# exporter change or scrape-config change is needed. k8s/roles/node-exporter/CLAUDE.md, *The
# textfile collector directory*, names this file's readers and the contract it follows.
#
# Read-only observation, deliberately: this does not add or change any cgroup bound.
#
# Usage: claude-cgroup-metrics.sh (no arguments)
# Runs as claude-cgroup-metrics.service, fired by claude-cgroup-metrics.timer every 30 seconds,
# and writes $TEXTFILE_DIR/claude_cgroup.prom. Environment, all optional: TEXTFILE_DIR (default
# /var/lib/node-exporter-textfile), CGROOT (default /sys/fs/cgroup) and PROCROOT (default /proc).
# It exits 0 without writing when TEXTFILE_DIR does not exist.
set -uo pipefail

for arg in "$@"; do
  case "$arg" in
    -h | --help) awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"; exit 0 ;;
  esac
done

# Overridable so the paired test can point these at a fixture tree; defaults are the real paths.
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node-exporter-textfile}
CGROOT=${CGROOT:-/sys/fs/cgroup}
PROCROOT=${PROCROOT:-/proc}
OUT="$TEXTFILE_DIR/claude_cgroup.prom"

# Skip silently on a host without the textfile hook (e.g. before node-exporter is deployed there).
[ -d "$TEXTFILE_DIR" ] || exit 0

# label -> cgroup path relative to CGROOT
declare -A CGROUPS=(
  # user.slice carries the ONE MemoryHigh/MemorySwapMax for the fleet; its memory.events counters
  # include both planes.
  [fleet]=user.slice
  [user-1000-slice]=user.slice/user-1000.slice
)

# Resolve the path rather than hardcode it: claude-rc.service sits under user.slice or system.slice,
# and a wrong guess leaves a green scrape with one plane missing. The label stays claude-rc on every
# host.
for candidate in user.slice/claude-rc.service system.slice/claude-rc.service \
  user.slice/homelab-claude-rc.service system.slice/homelab-claude-rc.service; do
  if [ -d "$CGROOT/$candidate" ]; then
    CGROUPS[claude-rc]=$candidate
    break
  fi
done

TMP=$(mktemp "$TEXTFILE_DIR/claude_cgroup.prom.XXXXXX") || exit 1
trap 'rm -f "$TMP"' EXIT

{
  printf '# HELP claude_cgroup_memory_current_bytes Current memory.current for the cgroup.\n'
  printf '# TYPE claude_cgroup_memory_current_bytes gauge\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/memory.current"
    [ -r "$f" ] && printf 'claude_cgroup_memory_current_bytes{cgroup="%s"} %s\n' "$label" "$(cat "$f")"
  done

  printf '# HELP claude_cgroup_memory_swap_current_bytes Current memory.swap.current for the cgroup.\n'
  printf '# TYPE claude_cgroup_memory_swap_current_bytes gauge\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/memory.swap.current"
    [ -r "$f" ] && printf 'claude_cgroup_memory_swap_current_bytes{cgroup="%s"} %s\n' "$label" "$(cat "$f")"
  done

  # high/max/oom/oom_kill are cumulative counters; `high` counts every MemoryHigh throttle.
  printf '# HELP claude_cgroup_memory_events_total memory.events counters (low/high/max/oom/oom_kill) for the cgroup.\n'
  printf '# TYPE claude_cgroup_memory_events_total counter\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/memory.events"
    [ -r "$f" ] || continue
    while read -r event count; do
      [ -n "$event" ] || continue
      printf 'claude_cgroup_memory_events_total{cgroup="%s",event="%s"} %s\n' "$label" "$event" "$count"
    done < "$f"
  done

  # memory.events counts descendants too, so the fleet's `high` includes each fan-out batch's own
  # MemoryHigh.
  printf '# HELP claude_cgroup_memory_events_local_total memory.events.local counters: events from the cgroup'"'"'s own limits, not its descendants'"'"'.\n'
  printf '# TYPE claude_cgroup_memory_events_local_total counter\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/memory.events.local"
    [ -r "$f" ] || continue
    while read -r event count; do
      [ -n "$event" ] || continue
      printf 'claude_cgroup_memory_events_local_total{cgroup="%s",event="%s"} %s\n' "$label" "$event" "$count"
    done < "$f"
  done

  # Cumulative stalled-time counters (`total=`), not avg10/60/300: a counter survives rate(), as
  # node_pressure_* does.
  printf '# HELP claude_cgroup_memory_pressure_stalled_usec_total PSI memory.pressure cumulative stalled microseconds for the cgroup.\n'
  printf '# TYPE claude_cgroup_memory_pressure_stalled_usec_total counter\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/memory.pressure"
    [ -r "$f" ] || continue
    while read -r kind rest; do
      [ -n "$kind" ] || continue
      total="${rest##*total=}"
      printf 'claude_cgroup_memory_pressure_stalled_usec_total{cgroup="%s",kind="%s"} %s\n' "$label" "$kind" "$total"
    done < "$f"
  done

  printf '# HELP claude_cgroup_cpu_usage_usec_total Cumulative CPU usage (cpu.stat usage_usec) for the cgroup.\n'
  printf '# TYPE claude_cgroup_cpu_usage_usec_total counter\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/cpu.stat"
    [ -r "$f" ] || continue
    usage=$(awk '$1=="usage_usec"{print $2}' "$f")
    [ -n "$usage" ] && printf 'claude_cgroup_cpu_usage_usec_total{cgroup="%s"} %s\n' "$label" "$usage"
  done

  printf '# HELP claude_cgroup_pids_current Current pids.current for the cgroup — the process-count signal (#1154: 203 during the stall).\n'
  printf '# TYPE claude_cgroup_pids_current gauge\n'
  for label in "${!CGROUPS[@]}"; do
    f="$CGROOT/${CGROUPS[$label]}/pids.current"
    [ -r "$f" ] && printf 'claude_cgroup_pids_current{cgroup="%s"} %s\n' "$label" "$(cat "$f")"
  done

  # cAdvisor reports kubepods.slice but never system.slice, where k3s runs.
  # A separate family keeps the homelab slices out of the ClaudeCgroup* alerts, which select by
  # name.
  printf '# HELP claude_host_plane_memory_bytes Memory of each top-level plane: anon, file and kernel from memory.stat, swap from memory.swap.current.\n'
  printf '# TYPE claude_host_plane_memory_bytes gauge\n'
  for plane in user kubepods system; do
    d="$CGROOT/$plane.slice"
    [ -r "$d/memory.stat" ] || continue
    awk -v p="$plane" '
      $1 == "anon" || $1 == "file" || $1 == "kernel" {
        printf "claude_host_plane_memory_bytes{plane=\"%s\",kind=\"%s\"} %s\n", p, $1, $2
      }' < "$d/memory.stat"
    [ -r "$d/memory.swap.current" ] &&
      printf 'claude_host_plane_memory_bytes{plane="%s",kind="swap"} %s\n' "$plane" "$(cat "$d/memory.swap.current")"
  done

  # memory.current counts page cache, so anon and file are split; anon is what the reservation
  # covers.
  # A stopped unit's cgroup goes away, so its last sample is the one before.
  batch_dirs=("$CGROOT"/user.slice/user-*.slice/user@*.service/*/fanout-*.service)
  batch_label() { local unit=${1##*/}; unit=${unit#fanout-}; printf '%s' "${unit%.service}"; }

  printf '# HELP claude_fanout_batch_memory_bytes A fan-out batch unit'"'"'s memory: anon and file from memory.stat, peak from memory.peak.\n'
  printf '# TYPE claude_fanout_batch_memory_bytes gauge\n'
  for d in "${batch_dirs[@]}"; do
    [ -d "$d" ] || continue
    b=$(batch_label "$d")
    # The shell opens the file, so a unit stopping mid-scan fails the redirect quietly instead of
    # making awk fatal.
    awk -v b="$b" '
      $1 == "anon" || $1 == "file" {
        printf "claude_fanout_batch_memory_bytes{batch=\"%s\",kind=\"%s\"} %s\n", b, $1, $2
      }' 2>/dev/null < "$d/memory.stat"
    peak=$(cat "$d/memory.peak" 2>/dev/null)
    [ -n "$peak" ] && printf 'claude_fanout_batch_memory_bytes{batch="%s",kind="peak"} %s\n' "$b" "$peak"
  done

  printf '# HELP claude_fanout_batch_cpu_usage_usec_total Cumulative CPU usage (cpu.stat usage_usec) of a fan-out batch unit.\n'
  printf '# TYPE claude_fanout_batch_cpu_usage_usec_total counter\n'
  for d in "${batch_dirs[@]}"; do
    [ -d "$d" ] || continue
    usage=$(awk '$1=="usage_usec"{print $2}' 2>/dev/null < "$d/cpu.stat")
    [ -n "$usage" ] && printf 'claude_fanout_batch_cpu_usage_usec_total{batch="%s"} %s\n' "$(batch_label "$d")" "$usage"
  done

  printf '# HELP claude_fanout_batch_pids_current Current pids.current of a fan-out batch unit.\n'
  printf '# TYPE claude_fanout_batch_pids_current gauge\n'
  for d in "${batch_dirs[@]}"; do
    [ -d "$d" ] || continue
    pids=$(cat "$d/pids.current" 2>/dev/null)
    [ -n "$pids" ] && printf 'claude_fanout_batch_pids_current{batch="%s"} %s\n' "$(batch_label "$d")" "$pids"
  done

  # cat reads the files, not awk: a path that vanishes between glob and read is fatal to gawk, which
  # made the count empty and the series a false 0. Name: is the first line, so it resets the record.
  if [[ -n "${CLAUDE_WATCH_UIDS:-}" ]]; then
    printf '# HELP claude_uid_processes Processes named claude whose real uid is this watched uid.\n'
    printf '# TYPE claude_uid_processes gauge\n'
    counts=$(cat "$PROCROOT"/[0-9]*/status 2>/dev/null | awk '
      $1 == "Name:" { name = $2 }
      $1 == "Uid:" && name == "claude" { n[$2]++ }
      END { for (u in n) print u, n[u] }
    ')
    for uid in $CLAUDE_WATCH_UIDS; do
      n=$(awk -v u="$uid" '$1 == u { print $2 }' <<< "$counts")
      printf 'claude_uid_processes{uid="%s"} %s\n' "$uid" "${n:-0}"
    done
  fi
} > "$TMP"

# 0644: node-exporter's container reads this as a non-root user, and mktemp creates 0600.
chmod 0644 "$TMP" && mv "$TMP" "$OUT"
