---
generated_from: scripts/docs/gen_reference_crons.py
generated_at: 2026-08-28 06:17 UTC
generated_sha: eb6d0cd2
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/gen_reference_crons.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Scheduled jobs

35 cron entrie(s) installed across the roles.

!!! warning "The state column is a heuristic"
    It is judged from the command text, and nothing in a cron task declares its own blast radius. A job that runs a wrapper script reads as "read the script" rather than being guessed at. Treat it as a pointer, not an authority.

| Job | Schedule | Host | User | Changes state | Defined in |
|---|---|---|---|---|---|
| Claude Code telemetry health | `{{ claude_otel_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/k8s/claude-otel/tasks/main.yml` |
| Clean unused Docker images | `30 6 * * *` | conditional (has_docker) | `{{ ansible_facts.user_id }}` | yes (prune) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Clear ansible log file | `0 6 * * 0` | every host in the play | `root` | yes (truncate) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Cloudflare IP drift | `25 5 * * *` | conditional (traefik_k8s_manage_cloudflare_drift_check | bool) | `root` | read the script | `ansible/roles/k8s/traefik/tasks/main.yml` |
| CrowdSec AppSec verify | `*/15 * * * *` | every host in the play | `root` | read the script | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| CrowdSec home allowlist | `*/5 * * * *` | every host in the play | `root` | read the script | `ansible/roles/k8s/crowdsec/tasks/main.yml` |
| Daily secret rotation audit | `0 8 * * *` | the gitops host | `{{ sys_user }}` | read the script | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Live object drift check | `{{ k3s_live_drift_cron_minute }} {{ k3s_live_drift_cron_hour }} * * *` | every host in the play | `{{ sys_user }}` | no (read-only by its command) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Longhorn backup health | `{{ k3s_longhorn_backup_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | yes (backup) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Longhorn filesystem trim | `{{ k3s_longhorn_trim_cron_minute }} {{ k3s_longhorn_trim_cron_hour }} * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Longhorn restore drill | `{{ k3s_longhorn_restore_drill_cron.split()[0] }} {{ k3s_longhorn_restore_drill_cron.split()[1] }} {{ k3s_longhorn_restore_drill_cron.split()[2] }} * *` | every host in the play | `root` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Manifest prune drift check | `{{ k3s_manifest_prune_cron_minute }} {{ k3s_manifest_prune_cron_hour }} * * *` | every host in the play | `root` | yes (prune) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Off-box etcd snapshot | `{{ k3s_etcd_s3_cron_minute }} {{ k3s_etcd_s3_cron_hour }} * * *` | every host in the play | `root` | yes (snapshot) | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| Pi SD-card health heartbeat | `*/5 * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/optimize_pi/tasks/main.yml` |
| Pi container-recovery heartbeat | `*/5 * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/optimize_pi/tasks/main.yml` |
| Redeploy {{ container_item.name }} | `{{ common_redeploy_cron_minute }} 6 * * 0` | every host in the play | `{{ sys_user }}` | yes (deploy) | `ansible/roles/containers/common/tasks/redeploy_cron.yml` |
| Refresh generated docs | `17 6,18 * * *` | daniel-box | `{{ sys_user }}` | read the script | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Refresh homelab infrastructure map | `*/15 * * * *` | daniel-box | `{{ sys_user }}` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Registry garbage collection | `{{ registry_k8s_gc_cron_minute }} {{ registry_k8s_gc_cron_hour }} * * {{ registry_k8s_gc_cron_weekday }}` | every host in the play | `root` | read the script | `ansible/roles/k8s/registry/tasks/main.yml` |
| Sync peer Claude artifacts | `{{ artifacts_sync_minute }} * * * *` | conditional (not k8s_dry_run | bool) | `{{ sys_user }}` | read the script | `ansible/roles/k8s/artifacts/tasks/main.yml` |
| Weekly AIDE file integrity check | `0 3 * * 1` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/integrity.yml` |
| Weekly apt autoremove | `0 2 * * 0` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/accounting.yml` |
| Weekly dpkg purge orphaned configs | `15 2 * * 0` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/accounting.yml` |
| Weekly firmware update | `0 7 * * 0` | conditional (initial_setup_fwupdmgr.stat.exists) | `root` | yes (reboot) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Weekly rkhunter malware scan | `0 2 * * 3` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/integrity.yml` |
| Weekly secret rotation (auto tier) | `0 9 * * 0` | the gitops host | `{{ sys_user }}` | yes (rotate) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| Weekly system restart | `30 7 * * 0` | every host in the play | `root` | no (read-only by its command) | `ansible/roles/setup/initial_setup/tasks/crons.yml` |
| configarr sync health | `{{ configarr_k8s_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/k8s/configarr/tasks/main.yml` |
| daniel-box disk health | `{{ k3s_disk_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |
| fake-remux health | `{{ fake_remux_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux reconcile | `{{ fake_remux_replace_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| fake-remux scan | `{{ fake_remux_scan_cron_minute }} {{ fake_remux_scan_cron_hour }} * * *` | every host in the play | `{{ sys_user }}` | no (read-only by its command) | `ansible/roles/setup/fake_remux/tasks/main.yml` |
| janitorr error health | `{{ janitorr_k8s_health_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/k8s/janitorr/tasks/main.yml` |
| qbittorrent prefs drift check | `{{ qbittorrent_k8s_prefs_check_cron_minute }} {{ qbittorrent_k8s_prefs_check_cron_hour }} * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/k8s/qbittorrent/tasks/main.yml` |
| remember log rotation health | `{{ k3s_remember_logs_cron_minute }} * * * *` | every host in the play | `{{ sys_user }}` | read the script | `ansible/roles/setup/k3s/tasks/health-crons.yml` |

## Schedule format

Five fields: minute, hour, day-of-month, month, day-of-week. A value still showing `{{ ... }}` is an Ansible variable that only resolves at deploy time — these pages are rendered by static parsing and never run Ansible, so the template is the honest rendering.
