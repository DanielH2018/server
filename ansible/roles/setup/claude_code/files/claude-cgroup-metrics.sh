#!/usr/bin/env bash
# Cgroup resource metrics for claude-rc.service and user-1000.slice — managed by Ansible
# (claude_code role); edits overwritten.
#
# Issue #1238: claude-rc.service is bounded (MemoryHigh/MemorySwapMax) and user-1000.slice
# (sessions started with `claude agents` from an SSH shell, #1213) is not — but neither
# cgroup's counters reached Prometheus. This host sets DefaultMemoryAccounting/
# CPUAccounting=yes, so the counters are already populated on cgroupfs and free to read; only
# the scrape was missing. Written as a node-exporter textfile gauge set so no exporter change
# or scrape-config change is needed (roles/k8s/node-exporter/CLAUDE.md: "an empty hook, not
# yet used").
#
# Read-only observation, deliberately: this does not add or change any cgroup bound (#1213
# owns the SSH-session cap fix; that is a separate decision).
set -uo pipefail

# Overridable so the paired test below can point this at a fixture tree instead of the real
# cgroupfs; unset in production, where both default to the real paths.
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node-exporter-textfile}
CGROOT=${CGROOT:-/sys/fs/cgroup}
OUT="$TEXTFILE_DIR/claude_cgroup.prom"

# Skip silently on a host without the textfile hook (e.g. before node-exporter is deployed
# there) — matches the guard the kopia-era b2-usage.sh used for the same directory.
[ -d "$TEXTFILE_DIR" ] || exit 0

# label -> cgroup path relative to CGROOT
declare -A CGROUPS=(
  [claude-rc]=system.slice/claude-rc.service
  [user-1000-slice]=user.slice/user-1000.slice
)

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

  # high/max/oom/oom_kill are cumulative counters: `high` is the one #1238 asks for by name —
  # it counts every time MemoryHigh throttled the cgroup, which is the number that would have
  # narrated the 2026-09-05 stall as it happened.
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

  # PSI: cumulative stalled-time counters (the `total=` field), not the kernel's own
  # avg10/60/300 — a counter survives a `rate()` the way the kernel's own decaying averages do
  # not, and matches how node_pressure_* already exposes PSI host-wide.
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
} > "$TMP"

# 0644: node-exporter's container reads this as its own (non-root) user, same as the
# kopia-era b2-usage.sh gauge — mktemp creates 0600 and the collector would otherwise get
# "permission denied" (node_textfile_scrape_error 1).
chmod 0644 "$TMP" && mv "$TMP" "$OUT"
