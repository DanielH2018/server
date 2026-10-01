---
generated_from: scripts/docs/reference/crons.py
generated_at: 2026-10-01 00:11 UTC
generated_sha: 7f5047711
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/crons.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scheduled jobs

41 cron entrie(s) installed across the roles.

!!! warning "The state column is a heuristic"
    It is judged from the command text, and nothing in a cron task declares its own blast radius. A job that runs a wrapper script reads as "read the script" rather than being guessed at. Treat it as a pointer, not an authority.

| Job | Schedule | Host | User | Changes state | Defined in |
|---|---|---|---|---|---|
| B2 backup budget listing | `20 7 * * *` | conditional (has_repo_checkout) | `ubuntu` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| B2 deletion accounting | `10 7 * * *` | conditional (has_repo_checkout) | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Claude Code telemetry health | `*/10 * * * *` | conditional (not k8s_dry_run \| bool) | `ubuntu` | read the script | `ansible/roles/k8s/observability/tasks/main.yml` |
| Clean unused Docker images | `30 6 * * *` | conditional (has_docker) | `{{ ansible_facts.user_id }}` | yes (prune) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Clear ansible log file | `0 6 * * 0` | conditional (has_repo_checkout) | `root` | yes (truncate) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| CrowdSec AppSec verify | `*/15 * * * *` | conditional (not k8s_dry_run \| bool) | `root` | read the script | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| CrowdSec bouncer prune | `17 * * * *` | conditional (not k8s_dry_run \| bool) | `root` | yes (prune) | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| CrowdSec home allowlist | `*/5 * * * *` | conditional (not k8s_dry_run \| bool) | `root` | read the script | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| CrowdSec remote allowlist | `*/5 * * * *` | conditional (not k8s_dry_run \| bool) | `root` | read the script | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| Full etcd restore drill in a throwaway guest | `20 11 1 * *` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/hypervisor/tasks/etcd_drill.yml` |
| Homelab eval sweep | `0 2 * * 0` | daniel-box | `ubuntu` | read the script | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Longhorn backup health | `*/10 * * * *` | every host in the play | `ubuntu` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Longhorn filesystem trim | `10 6 * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Longhorn restore drill | `10 4 * * *` | every host in the play | `root` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Off-box etcd snapshot | `45 2 * * *` | every host in the play | `root` | yes (snapshot) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Pi SD-card health heartbeat | `*/5 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/optimize_pi/tasks/main.yml` |
| Pi container-recovery heartbeat | `*/5 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/optimize_pi/tasks/main.yml` |
| Pi rotated-log integrity sweep | `40 1 * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/optimize_pi/tasks/main.yml` |
| Refresh generated docs | `17 6,18 * * *` | daniel-box | `ubuntu` | read the script | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Refresh homelab infrastructure map | `*/15 * * * *` | daniel-box | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Release staleness drift check | `*/30 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Sync peer Claude artifacts | `*/5 * * * *` | conditional (not k8s_dry_run \| bool) | `ubuntu` | read the script | `ansible/roles/k8s/artifacts/tasks/main.yml` |
| TLS cert-expiry watch | `10 5 * * *` | daniel-box | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| UPS secondary watchdog | `*/10 * * * *` | conditional (nut_host_watchdog_armed \| bool) | `root` | read the script | `ansible/roles/setup/nut_host/tasks/main.yml` |
| Weekly AIDE file integrity check | `0 3 * * 1` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/integrity.yml` |
| Weekly apt autoremove | `0 2 * * 0` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/accounting.yml` |
| Weekly dpkg purge orphaned configs | `15 2 * * 0` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/accounting.yml` |
| Weekly firmware update | `0 7 * * 0` | conditional (initial_setup_fwupdmgr.stat.exists) | `root` | yes (reboot) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Weekly git object-store repair | `20 4 * * 0` | daniel-box | `ubuntu` | yes (prune) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Weekly rkhunter malware scan | `0 2 * * 3` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/integrity.yml` |
| Weekly secret rotation (auto tier) | `0 9 * * 0` | the gitops host | `ubuntu` | yes (rotate) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Weekly system restart | `30 7 * * 0` | every host in the play | `root` | yes (reboot, restart) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| configarr sync health | `*/10 * * * *` | conditional (not k8s_dry_run \| bool) | `ubuntu` | read the script | `ansible/roles/k8s/configarr/tasks/main.yml` |
| daniel-box disk health | `*/10 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| etcd restore drill | `20 10 * * 1` | conditional (has_repo_checkout) | `root` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| fake-remux health | `*/10 * * * *` | every host in the play | `ubuntu` | read the script | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux reconcile | `*/20 * * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux scan | `45 4 * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| janitorr error health | `*/10 * * * *` | conditional (not k8s_dry_run \| bool) | `ubuntu` | read the script | `ansible/roles/k8s/janitorr/tasks/main.yml` |
| mkv attachment repair | `*/15 * * * *` | every host in the play | `ubuntu` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| qbittorrent prefs drift check | `37 6 * * *` | conditional (not k8s_dry_run \| bool) | `ubuntu` | read the script | `ansible/roles/k8s/qbittorrent/tasks/main.yml` |

## Schedule format

Five fields: minute, hour, day-of-month, month, day-of-week. A schedule or user shows the value every host resolves when its variables come from role defaults or `group_vars/all.yml` and no host overrides them. A value still showing `{{ ... }}` differs by host or does not evaluate statically, so it only resolves at deploy time, and the template is the honest rendering.
