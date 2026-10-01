---
generated_from: scripts/docs/reference/freshness.py
generated_at: 2026-10-01 06:17 UTC
generated_sha: 89bf63ff9
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/freshness.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Doc freshness

71 hand-written page(s). *Changed* is the page's last commit; *moved* counts the repo files the page names whose last commit is later than that. A moved source does not prove the page is wrong -- it marks the page to reread next. The generated reference pages are not listed: they are rebuilt from the tree.

| Page | Changed | Sources named | Moved since | Most recently moved |
|---|---|---|---|---|
| [break-glass.md](../break-glass.md) | 2026-09-17 | 17 | 15 | `docs/longhorn-disaster-recovery.md` (2026-10-01) |
| [issue-claiming-and-fanout.md](../issue-claiming-and-fanout.md) | 2026-09-26 | 22 | 11 | `docs/reference/backlog.md` (2026-10-01) |
| [k3s-upgrade.md](../k3s-upgrade.md) | 2026-09-26 | 13 | 9 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-10-01) |
| [k3s-etcd-restore.md](../k3s-etcd-restore.md) | 2026-09-24 | 9 | 7 | `ansible/inventory/group_vars/all.yml` (2026-10-01) |
| [secret-rotation.md](../secret-rotation.md) | 2026-09-27 | 13 | 7 | `ansible/roles/k8s/crowdsec/CLAUDE.md` (2026-10-01) |
| [monitor-bridge-internals.md](../monitor-bridge-internals.md) | 2026-09-29 | 20 | 7 | `docs/monitor-bridge-checks.md` (2026-10-01) |
| [python-code-organization.md](../python-code-organization.md) | 2026-09-30 | 65 | 7 | `ansible/roles/setup/gitops_deploy/files/deploy_io.py` (2026-10-01) |
| [longhorn-upgrade.md](../longhorn-upgrade.md) | 2026-09-21 | 9 | 5 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-10-01) |
| [gitops-pipeline.md](../gitops-pipeline.md) | 2026-09-30 | 84 | 5 | `ansible/roles/k8s/manifests/tasks/release_stamp.yml` (2026-10-01) |
| [adr/0003-sops-with-age-for-secrets-at-rest.md](../adr/0003-sops-with-age-for-secrets-at-rest.md) | 2026-08-24 | 4 | 4 | `ansible/vars/secrets.yml` (2026-09-30) |
| [adr/0001-mkdocs-site-with-generated-reference.md](../adr/0001-mkdocs-site-with-generated-reference.md) | 2026-09-02 | 4 | 4 | `CLAUDE.md` (2026-09-30) |
| [adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md](../adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) | 2026-09-24 | 5 | 4 | `scripts/deploy_tools/deploy_tags.py` (2026-09-30) |
| [claude-shell-permissions.md](../claude-shell-permissions.md) | 2026-09-24 | 7 | 4 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-10-01) |
| [game-stats-internals.md](../game-stats-internals.md) | 2026-09-29 | 7 | 4 | `ansible/roles/k8s/game-stats/CLAUDE.md` (2026-10-01) |
| [authelia-sessions-and-crowdsec-init.md](../authelia-sessions-and-crowdsec-init.md) | 2026-09-30 | 18 | 4 | `ansible/roles/k8s/authelia/CLAUDE.md` (2026-10-01) |
| [host-baseline-record.md](../host-baseline-record.md) | 2026-09-30 | 33 | 4 | `prek.toml` (2026-10-01) |
| [anilist-integration.md](../anilist-integration.md) | 2026-09-06 | 3 | 3 | `ansible/roles/k8s/manifests/tasks/main.yml` (2026-10-01) |
| [adr/0011-one-lock-serialises-every-deploy-path.md](../adr/0011-one-lock-serialises-every-deploy-path.md) | 2026-09-12 | 3 | 3 | `ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` (2026-09-30) |
| [uptime-robot-monitors.md](../uptime-robot-monitors.md) | 2026-09-28 | 5 | 3 | `docs/longhorn-disaster-recovery.md` (2026-10-01) |
| [autofix-bridge-actuators.md](../autofix-bridge-actuators.md) | 2026-09-29 | 10 | 3 | `ansible/inventory/host_vars/daniel-box.yml` (2026-10-01) |
| [k3s-node-plane-crons-and-incidents.md](../k3s-node-plane-crons-and-incidents.md) | 2026-09-29 | 11 | 3 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [qbittorrent-vpn-and-prefs.md](../qbittorrent-vpn-and-prefs.md) | 2026-09-29 | 9 | 3 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [volume-revert-drill-and-sizing.md](../volume-revert-drill-and-sizing.md) | 2026-09-29 | 7 | 3 | `ansible/tests/longhorn/test_volume_revert_input_guard.py` (2026-10-01) |
| [uptime-kuma-autokuma-record.md](../uptime-kuma-autokuma-record.md) | 2026-09-30 | 22 | 3 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [adr/0015-d2-for-hand-authored-diagrams.md](../adr/0015-d2-for-hand-authored-diagrams.md) | 2026-09-01 | 2 | 2 | `scripts/docs/build_docs.py` (2026-09-24) |
| [adr/0018-the-repository-stays-public-and-ci-stays-hosted.md](../adr/0018-the-repository-stays-public-and-ci-stays-hosted.md) | 2026-09-28 | 4 | 2 | `ansible/vars/secrets.yml` (2026-09-30) |
| [cronjob-gate-design.md](../cronjob-gate-design.md) | 2026-09-29 | 5 | 2 | `ansible/inventory/group_vars/all.yml` (2026-10-01) |
| [homepage-widgets-and-layout.md](../homepage-widgets-and-layout.md) | 2026-09-29 | 10 | 2 | `ansible/roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2` (2026-10-01) |
| [n8n-community-packages.md](../n8n-community-packages.md) | 2026-09-29 | 4 | 2 | `scripts/diagnostics/probe_lib/core.py` (2026-10-01) |
| [deploying.md](../deploying.md) | 2026-09-30 | 5 | 2 | `ansible/tests/deploy/test_k8s_dry_run_host_writes.py` (2026-10-01) |
| [landing.md](../landing.md) | 2026-09-30 | 19 | 2 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | 2026-08-24 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [adr/0005-traefik-is-the-edge-with-ingressroute-crds.md](../adr/0005-traefik-is-the-edge-with-ingressroute-crds.md) | 2026-08-24 | 1 | 1 | `ansible/templates/ingressroute.yml.j2` (2026-09-30) |
| [adr/0007-backup-tiering-r2-daily-b2-weekly.md](../adr/0007-backup-tiering-r2-daily-b2-weekly.md) | 2026-08-24 | 1 | 1 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-10-01) |
| [adr/0009-networkpolicy-default-deny-ingress.md](../adr/0009-networkpolicy-default-deny-ingress.md) | 2026-08-24 | 1 | 1 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-10-01) |
| [adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | 2026-08-25 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [adr/0006-longhorn-for-cluster-storage.md](../adr/0006-longhorn-for-cluster-storage.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-10-01) |
| [adr/0008-16-mib-longhorn-blocks.md](../adr/0008-16-mib-longhorn-blocks.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-10-01) |
| [security-tools.md](../security-tools.md) | 2026-09-02 | 3 | 1 | `ansible/initial_setup.yml` (2026-09-28) |
| [wireguard-private-homelab-access.md](../wireguard-private-homelab-access.md) | 2026-09-03 | 2 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-10-01) |
| [email-to-rss.md](../email-to-rss.md) | 2026-09-21 | 1 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-10-01) |
| [adr/0013-daniel-pi-stays-on-docker.md](../adr/0013-daniel-pi-stays-on-docker.md) | 2026-09-29 | 1 | 1 | `ansible/inventory/host_vars/daniel-pi.yml` (2026-09-30) |
| [valheim-modding.md](../valheim-modding.md) | 2026-09-29 | 1 | 1 | `ansible/roles/k8s/valheim/CLAUDE.md` (2026-09-30) |
| [adr/0010-pull-based-gitops-over-argo-and-flux.md](../adr/0010-pull-based-gitops-over-argo-and-flux.md) | 2026-09-30 | 4 | 1 | `scripts/validate/k8s_manifests.py` (2026-10-01) |
| [failure-classes.md](../failure-classes.md) | 2026-09-30 | 17 | 1 | `docs/reference/backlog.md` (2026-10-01) |
| [healthchecks-io-deadman.md](../healthchecks-io-deadman.md) | 2026-09-30 | 18 | 1 | `ansible/roles/setup/k3s/templates/manifest-prune-check.sh.j2` (2026-10-01) |
| [hypervisor-libvirt-internals.md](../hypervisor-libvirt-internals.md) | 2026-09-30 | 13 | 1 | `scripts/secrets_mgmt/consumers.py` (2026-10-01) |
| [pi-host-tuning-record.md](../pi-host-tuning-record.md) | 2026-09-30 | 14 | 1 | `scripts/diagnostics/probe.py` (2026-10-01) |
| [renovate-agent-bounds-and-digest.md](../renovate-agent-bounds-and-digest.md) | 2026-09-30 | 21 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-10-01) |
| [adr/0004-authelia-is-the-single-sign-on-layer.md](../adr/0004-authelia-is-the-single-sign-on-layer.md) | 2026-09-02 | 0 | 0 | — |
| [adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md](../adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | 2026-09-28 | 3 | 0 | — |
| [adr/index.md](../adr/index.md) | 2026-09-28 | 1 | 0 | — |
| [index.md](../index.md) | 2026-09-28 | 0 | 0 | — |
| [claude-code-rc-caps.md](../claude-code-rc-caps.md) | 2026-09-29 | 4 | 0 | — |
| [jellyfin-plugins.md](../jellyfin-plugins.md) | 2026-09-29 | 2 | 0 | — |
| [traefik-plugins-and-startup.md](../traefik-plugins-and-startup.md) | 2026-09-29 | 6 | 0 | — |
| [adr/0016-code-scanning-stays-on-default-setup.md](../adr/0016-code-scanning-stays-on-default-setup.md) | 2026-09-30 | 9 | 0 | — |
| [renovate-notify-internals.md](../renovate-notify-internals.md) | 2026-09-30 | 8 | 0 | — |
| [claude-tooling.md](../claude-tooling.md) | 2026-10-01 | 46 | 0 | — |
| [crowdsec-waf-record.md](../crowdsec-waf-record.md) | 2026-10-01 | 6 | 0 | — |
| [docker-engine-pins-and-runtime.md](../docker-engine-pins-and-runtime.md) | 2026-10-01 | 6 | 0 | — |
| [headlamp-oidc-and-plugins.md](../headlamp-oidc-and-plugins.md) | 2026-10-01 | 3 | 0 | — |
| [longhorn-backup-tiering.md](../longhorn-backup-tiering.md) | 2026-10-01 | 10 | 0 | — |
| [longhorn-disaster-recovery.md](../longhorn-disaster-recovery.md) | 2026-10-01 | 15 | 0 | — |
| [monitor-bridge-checks.md](../monitor-bridge-checks.md) | 2026-10-01 | 47 | 0 | — |
| [networkpolicy-default-deny.md](../networkpolicy-default-deny.md) | 2026-10-01 | 11 | 0 | — |
| [networkpolicy-slice-answers.md](../networkpolicy-slice-answers.md) | 2026-10-01 | 18 | 0 | — |
| [observability-dashboards.md](../observability-dashboards.md) | 2026-10-01 | 14 | 0 | — |
| [observability-oidc-and-idle-diagnosis.md](../observability-oidc-and-idle-diagnosis.md) | 2026-10-01 | 5 | 0 | — |
| [traefik-client-identity-and-tls.md](../traefik-client-identity-and-tls.md) | 2026-10-01 | 5 | 0 | — |
| [volume-snapshot-drills.md](../volume-snapshot-drills.md) | 2026-10-01 | 11 | 0 | — |
