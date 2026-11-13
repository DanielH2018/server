---
generated_from: scripts/docs/reference/crons.py
generated_at: 2026-11-12 18:40 UTC
generated_sha: 65e094073
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/crons.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scheduled jobs

53 job(s) scheduled by the host packages' timers.

!!! warning "The state column is a heuristic"
    It is judged from the command text, and nothing in a unit declares its own blast radius. A job that runs a wrapper script reads as "read the script" rather than being guessed at. Treat it as a pointer, not an authority.

| Job | Schedule | Host | User | Changes state | Defined in |
|---|---|---|---|---|---|
| homelab-aide-check.timer | `OnCalendar=Mon *-*-* 03:00:00` | daniel-box, daniel-server | `root` | read the script | `hosts/components/aide-scan/units/homelab-aide-check.timer` |
| homelab-apt-autoremove.timer | `OnCalendar=Sun *-*-* 02:00:00` | daniel-box, daniel-pi, daniel-server | `root` | read the script | `hosts/components/host-maintenance/units/homelab-apt-autoremove.timer` |
| homelab-b2-budget-listing.timer | `OnCalendar=*-*-* 7:20:00` | daniel-box | `ubuntu` | read the script | `hosts/components/k3s-host-checks/units/homelab-b2-budget-listing.timer.j2` |
| homelab-b2-deletion-accounting.timer | `OnCalendar=*-*-* 7:10:00` | daniel-box | `ubuntu` | read the script | `hosts/components/k3s-host-checks/units/homelab-b2-deletion-accounting.timer.j2` |
| homelab-builder.timer | `OnBootSec=2min, OnUnitActiveSec=2min` | daniel-box | `homelab-builder` | read the script | `hosts/components/builder/units/homelab-builder.timer` |
| homelab-checkout-sync.timer | `OnBootSec=10min, OnUnitActiveSec=10min` | daniel-box | `ubuntu` | read the script | `hosts/components/checkout-sync/units/homelab-checkout-sync.timer` |
| homelab-claude-agent-browser.timer | `OnBootSec=10min, OnUnitInactiveSec=6h` | daniel-box, daniel-server | `claude` | read the script | `hosts/components/claude-code/units/homelab-claude-agent-browser.timer` |
| homelab-claude-cgroup-metrics.timer | `OnBootSec=30s, OnUnitActiveSec=30s` | daniel-box, daniel-server | `root` | read the script | `hosts/components/claude-code/templates/claude-cgroup-metrics.timer.j2` |
| homelab-claude-clone-sync.timer | `OnBootSec=10min, OnUnitActiveSec=15min` | daniel-box, daniel-server | `claude` | read the script | `hosts/components/claude-code/templates/claude-clone-sync.timer.j2` |
| homelab-claude-dotfiles-sync.timer | `OnBootSec=10min, OnUnitActiveSec=15min` | daniel-box, daniel-server | `claude` | read the script | `hosts/components/claude-code/templates/claude-dotfiles-sync.timer.j2` |
| homelab-claude-fanout-server-lander.timer | `OnBootSec=5min, OnUnitActiveSec=10min` | daniel-box | `ubuntu` | read the script | `hosts/components/claude-code/templates/claude-fanout-server-lander.timer.j2` |
| homelab-claude-memory-sync.timer | `OnBootSec=5min, OnUnitActiveSec=15min` | daniel-box | `ubuntu` | yes (delete) | `hosts/components/claude-code/templates/claude-memory-sync.timer.j2` |
| homelab-claude-otel-health.timer | `OnCalendar=*-*-* *:00/10:00` | daniel-box | `ubuntu` | read the script | `hosts/components/claude-code/units/homelab-claude-otel-health.timer.j2` |
| homelab-claude-pipeline-timeline.timer | `OnBootSec=15min, OnUnitActiveSec=1440min` | daniel-box | `ubuntu` | read the script | `hosts/components/claude-code/templates/claude-pipeline-timeline.timer.j2` |
| homelab-claude-rc-restart.timer | `OnCalendar=weekly` | daniel-box, daniel-server (no enable link) | `root` | yes (restart) | `hosts/components/claude-code/templates/claude-rc-restart.timer.j2` |
| homelab-claude-triage-poll.timer | `OnBootSec=5min, OnUnitActiveSec=2min` | daniel-box | `claude` | read the script | `hosts/components/claude-code/templates/claude-triage-poll.timer.j2` |
| homelab-crowdsec-probe.timer | `OnBootSec=1min, OnUnitActiveSec=1min` | daniel-pi | `ubuntu` | read the script | `hosts/components/crowdsec-probe/units/homelab-crowdsec-probe.timer` |
| homelab-disk-health.timer | `OnCalendar=*:00/10` | daniel-box | `ubuntu` | read the script | `hosts/components/k3s-host-checks/units/homelab-disk-health.timer.j2` |
| homelab-docker-engine.timer | `OnCalendar=*-*-* 00:30:00` | daniel-pi | `root` | yes (apply) | `hosts/components/pi-docker/units/homelab-docker-engine.timer` |
| homelab-docker-prune.timer | `OnCalendar=*-*-* 06:30:00` | daniel-pi | `root` | yes (prune) | `hosts/components/host-maintenance/units/homelab-docker-prune.timer.j2` |
| homelab-docs-refresh.timer | `OnCalendar=*-*-* 06,18:17:00` | daniel-box | `ubuntu` | read the script | `hosts/components/docs-refresh/units/homelab-docs-refresh.timer` |
| homelab-dpkg-purge.timer | `OnCalendar=Sun *-*-* 02:15:00` | daniel-box, daniel-pi, daniel-server | `root` | read the script | `hosts/components/host-maintenance/units/homelab-dpkg-purge.timer` |
| homelab-etcd-restore-drill.timer | `OnCalendar=Mon *-*-* 10:20:00` | daniel-box | `root` | yes (backup) | `hosts/components/k3s-host-checks/units/homelab-etcd-restore-drill.timer.j2` |
| homelab-etcd-restore-drill.timer | `OnCalendar=*-*-01 11:20:00` | daniel-server | `root` | read the script | `hosts/components/k3s-host-checks/units/homelab-etcd-restore-drill.timer.j2` |
| homelab-etcd-token-drift.timer | `OnCalendar=*-*-* 2:45:00` | daniel-box | `root` | read the script | `hosts/components/k3s-host-checks/units/homelab-etcd-token-drift.timer.j2` |
| homelab-eval-sweep.timer | `OnCalendar=Sun *-*-* 02:00:00` | daniel-box | `ubuntu` | read the script | `hosts/components/eval-sweep/units/homelab-eval-sweep.timer` |
| homelab-fake-remux-health.timer | `OnCalendar=*:0/10:00` | daniel-box | `ubuntu` | read the script | `hosts/components/fake-remux/units/homelab-fake-remux-health.timer.j2` |
| homelab-fake-remux-reconcile.timer | `OnCalendar=*:0/20:00` | daniel-box | `ubuntu` | read the script | `hosts/components/fake-remux/units/homelab-fake-remux-reconcile.timer.j2` |
| homelab-fake-remux-scan.timer | `OnCalendar=*-*-* 04:45:00` | daniel-box | `ubuntu` | read the script | `hosts/components/fake-remux/units/homelab-fake-remux-scan.timer.j2` |
| homelab-firmware-update.timer | `OnCalendar=Sun *-*-* 07:00:00` | daniel-box | `root` | read the script | `hosts/components/host-maintenance/units/homelab-firmware-update.timer` |
| homelab-github-interaction-limit.timer | `OnCalendar=*-*-* 06:50:00` | daniel-box | `root` | read the script | `hosts/components/github-checks/units/homelab-github-interaction-limit.timer.j2` |
| homelab-github-ruleset-drift.timer | `OnCalendar=*-*-* 06:40:00` | daniel-box | `root` | read the script | `hosts/components/github-checks/units/homelab-github-ruleset-drift.timer.j2` |
| homelab-infra-map.timer | `OnCalendar=*-*-* *:00,15,30,45:00` | daniel-box | `ubuntu` | read the script | `hosts/components/infra-map/units/homelab-infra-map.timer` |
| homelab-k3s-restart.timer | `OnCalendar=Mon..Fri *-*-02..31 11:32:00` | daniel-box | `root` | yes (restart) | `hosts/components/k3s-restart/units/homelab-k3s-restart.timer.j2` |
| homelab-loki-route-witness.timer | `OnCalendar=*-*-* *:23:00` | daniel-box, daniel-server | `ubuntu` | read the script | `hosts/components/loki-route-witness/units/homelab-loki-route-witness.timer` |
| homelab-longhorn-backup-health.timer | `OnCalendar=*:00/10` | daniel-box | `ubuntu` | yes (backup) | `hosts/components/k3s-host-checks/units/homelab-longhorn-backup-health.timer.j2` |
| homelab-mkv-attachment-repair.timer | `OnCalendar=*:0/15:00` | daniel-box | `ubuntu` | read the script | `hosts/components/fake-remux/units/homelab-mkv-attachment-repair.timer.j2` |
| homelab-pi-gz-integrity.timer | `OnCalendar=*-*-* 01:40:00, OnActiveSec=1min` | daniel-pi | `ubuntu` | read the script | `hosts/components/pi-heartbeats/units/homelab-pi-gz-integrity.timer` |
| homelab-pi-recovery.timer | `OnCalendar=*:0/5` | daniel-pi | `ubuntu` | read the script | `hosts/components/pi-heartbeats/units/homelab-pi-recovery.timer` |
| homelab-pi-sd-health.timer | `OnCalendar=*:0/5` | daniel-pi | `ubuntu` | read the script | `hosts/components/pi-heartbeats/units/homelab-pi-sd-health.timer` |
| homelab-pi-watchdog-health.timer | `OnCalendar=*:0/5` | daniel-pi | `ubuntu` | read the script | `hosts/components/pi-heartbeats/units/homelab-pi-watchdog-health.timer` |
| homelab-reboot.timer | `OnCalendar=Sun *-*-* 07:30:00 UTC` | daniel-box, daniel-pi, daniel-server | `root` | yes (reboot) | `hosts/components/host-maintenance/units/homelab-reboot.timer.j2` |
| homelab-registry-gc.timer | `OnCalendar=Sun *-*-* 04:20:00` | daniel-box | `root` | read the script | `hosts/components/registry-gc/units/homelab-registry-gc.timer` |
| homelab-renovate-agent.timer | `OnCalendar=*-*-* 06:00:00 America/Chicago` | daniel-box | `renovate-agent` | read the script | `hosts/components/renovate-agent/templates/homelab-renovate-agent.timer.j2` |
| homelab-renovate-notify.timer | `OnCalendar=*-*-* 13:00:00` | daniel-box | `ubuntu` | read the script | `hosts/components/renovate-notify/units/homelab-renovate-notify.timer` |
| homelab-rkhunter-check.timer | `OnCalendar=Wed *-*-* 02:00:00` | daniel-box, daniel-pi, daniel-server | `root` | read the script | `hosts/components/integrity-scans/units/homelab-rkhunter-check.timer` |
| homelab-secret-rotation-audit.timer | `OnCalendar=*-*-* 08:30:00` | daniel-box | `ubuntu` | read the script | `hosts/components/secret-rotation/units/homelab-secret-rotation-audit.timer` |
| homelab-sync-artifacts.timer | `OnCalendar=*-*-* *:00/5:00` | daniel-box | `ubuntu` | read the script | `hosts/components/claude-code/units/homelab-sync-artifacts.timer.j2` |
| homelab-updater-poll.timer | `OnBootSec=15min, OnUnitActiveSec=15min` | daniel-pi | `root` | read the script | `hosts/components/pi-updater-poll/units/homelab-updater-poll.timer` |
| homelab-updater.timer | `OnBootSec=5min, OnUnitActiveSec=5min` | daniel-box, daniel-server | `root` | read the script | `hosts/packages/homelab-updater/package.yml` |
| homelab-updater.timer | `OnCalendar=*-*-* 03:05:00` | daniel-pi | `root` | read the script | `hosts/packages/homelab-updater/package.yml` |
| homelab-ups-watchdog.timer | `OnCalendar=*:0/10` | daniel-box, daniel-server | `root` | read the script | `hosts/components/nut-secondary/units/homelab-ups-watchdog.timer.j2` |
| homelab-worktree-sweep.timer | `OnCalendar=Sun *-*-* 04:20:00` | daniel-box, daniel-server | `ubuntu` | read the script | `hosts/components/worktree-sweep/units/homelab-worktree-sweep.timer` |

## Schedule format

A host package's timer shows its triggers as systemd reads them, each host's drop-ins applied. A host marked `(no enable link)` receives the unit and does not arm it.
