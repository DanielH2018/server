---
generated_from: scripts/docs/reference/freshness.py
generated_at: 2026-09-29 06:17 UTC
generated_sha: a3360b904
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/freshness.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Doc freshness

45 hand-written page(s). *Changed* is the page's last commit; *moved* counts the repo files the page names whose last commit is later than that. A moved source does not prove the page is wrong -- it marks the page to reread next. The generated reference pages are not listed: they are rebuilt from the tree.

| Page | Changed | Sources named | Moved since | Most recently moved |
|---|---|---|---|---|
| [gitops-pipeline.md](../gitops-pipeline.md) | 2026-09-28 | 80 | 18 | `ansible/roles/setup/gitops_deploy/files/deploy_logic.py` (2026-09-29) |
| [break-glass.md](../break-glass.md) | 2026-09-17 | 17 | 15 | `ansible/deploy.yml` (2026-09-29) |
| [networkpolicy-slice-answers.md](../networkpolicy-slice-answers.md) | 2026-09-03 | 18 | 12 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-29) |
| [issue-claiming-and-fanout.md](../issue-claiming-and-fanout.md) | 2026-09-26 | 22 | 10 | `scripts/dev/findings.py` (2026-09-28) |
| [python-code-organization.md](../python-code-organization.md) | 2026-09-28 | 65 | 10 | `ansible/roles/setup/gitops_deploy/files/deploy_toolbox.py` (2026-09-29) |
| [b2-transaction-cap-monitoring-gaps.md](../b2-transaction-cap-monitoring-gaps.md) | 2026-09-02 | 8 | 8 | `ansible/deploy.yml` (2026-09-29) |
| [k3s-upgrade.md](../k3s-upgrade.md) | 2026-09-26 | 13 | 8 | `ansible/roles/setup/gitops_deploy/files/deploy_changes.py` (2026-09-29) |
| [longhorn-backup-tiering.md](../longhorn-backup-tiering.md) | 2026-09-26 | 10 | 8 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-28) |
| [k3s-etcd-restore.md](../k3s-etcd-restore.md) | 2026-09-24 | 9 | 7 | `ansible/roles/setup/hypervisor/tasks/etcd_drill.yml` (2026-09-28) |
| [secret-rotation.md](../secret-rotation.md) | 2026-09-27 | 13 | 7 | `ansible/vars/secrets.yml` (2026-09-28) |
| [longhorn-upgrade.md](../longhorn-upgrade.md) | 2026-09-21 | 9 | 5 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-28) |
| [healthchecks-io-deadman.md](../healthchecks-io-deadman.md) | 2026-09-25 | 17 | 5 | `ansible/deploy.yml` (2026-09-29) |
| [adr/0003-sops-with-age-for-secrets-at-rest.md](../adr/0003-sops-with-age-for-secrets-at-rest.md) | 2026-08-24 | 4 | 4 | `ansible/vars/secrets.yml` (2026-09-28) |
| [adr/0001-mkdocs-site-with-generated-reference.md](../adr/0001-mkdocs-site-with-generated-reference.md) | 2026-09-02 | 4 | 4 | `CLAUDE.md` (2026-09-28) |
| [adr/0016-code-scanning-stays-on-default-setup.md](../adr/0016-code-scanning-stays-on-default-setup.md) | 2026-09-17 | 9 | 4 | `scripts/deploy_tools/land_tags.py` (2026-09-29) |
| [adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md](../adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) | 2026-09-24 | 5 | 4 | `scripts/deploy_tools/deploy_tags.py` (2026-09-29) |
| [claude-shell-permissions.md](../claude-shell-permissions.md) | 2026-09-24 | 7 | 4 | `CLAUDE.md` (2026-09-28) |
| [anilist-integration.md](../anilist-integration.md) | 2026-09-06 | 3 | 3 | `ansible/roles/k8s/manifests/tasks/main.yml` (2026-09-28) |
| [adr/0011-one-lock-serialises-every-deploy-path.md](../adr/0011-one-lock-serialises-every-deploy-path.md) | 2026-09-12 | 3 | 3 | `ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` (2026-09-29) |
| [networkpolicy-default-deny.md](../networkpolicy-default-deny.md) | 2026-09-18 | 11 | 3 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-28) |
| [adr/0015-d2-for-hand-authored-diagrams.md](../adr/0015-d2-for-hand-authored-diagrams.md) | 2026-09-01 | 2 | 2 | `scripts/docs/build_docs.py` (2026-09-24) |
| [failure-classes.md](../failure-classes.md) | 2026-09-24 | 15 | 2 | `docs/reference/backlog.md` (2026-09-28) |
| [adr/0010-pull-based-gitops-over-argo-and-flux.md](../adr/0010-pull-based-gitops-over-argo-and-flux.md) | 2026-09-28 | 3 | 2 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-29) |
| [volume-snapshot-drills.md](../volume-snapshot-drills.md) | 2026-09-28 | 7 | 2 | `ansible/roles/k8s/volume-snapshot/CLAUDE.md` (2026-09-29) |
| [adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | 2026-08-24 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-28) |
| [adr/0005-traefik-is-the-edge-with-ingressroute-crds.md](../adr/0005-traefik-is-the-edge-with-ingressroute-crds.md) | 2026-08-24 | 1 | 1 | `ansible/templates/ingressroute.yml.j2` (2026-09-18) |
| [adr/0007-backup-tiering-r2-daily-b2-weekly.md](../adr/0007-backup-tiering-r2-daily-b2-weekly.md) | 2026-08-24 | 1 | 1 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-28) |
| [adr/0009-networkpolicy-default-deny-ingress.md](../adr/0009-networkpolicy-default-deny-ingress.md) | 2026-08-24 | 1 | 1 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-28) |
| [adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | 2026-08-25 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-28) |
| [adr/0006-longhorn-for-cluster-storage.md](../adr/0006-longhorn-for-cluster-storage.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-05) |
| [adr/0008-16-mib-longhorn-blocks.md](../adr/0008-16-mib-longhorn-blocks.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-05) |
| [adr/0013-daniel-pi-stays-on-docker.md](../adr/0013-daniel-pi-stays-on-docker.md) | 2026-09-02 | 1 | 1 | `ansible/inventory/host_vars/daniel-pi.yml` (2026-09-24) |
| [security-tools.md](../security-tools.md) | 2026-09-02 | 3 | 1 | `ansible/initial_setup.yml` (2026-09-28) |
| [wireguard-private-homelab-access.md](../wireguard-private-homelab-access.md) | 2026-09-03 | 2 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-29) |
| [email-to-rss.md](../email-to-rss.md) | 2026-09-21 | 1 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-09-28) |
| [longhorn-disaster-recovery.md](../longhorn-disaster-recovery.md) | 2026-09-28 | 15 | 1 | `ansible/deploy.yml` (2026-09-29) |
| [uptime-robot-monitors.md](../uptime-robot-monitors.md) | 2026-09-28 | 5 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-29) |
| [adr/0004-authelia-is-the-single-sign-on-layer.md](../adr/0004-authelia-is-the-single-sign-on-layer.md) | 2026-09-02 | 0 | 0 | — |
| [adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md](../adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | 2026-09-28 | 3 | 0 | — |
| [adr/0018-the-repository-stays-public-and-ci-stays-hosted.md](../adr/0018-the-repository-stays-public-and-ci-stays-hosted.md) | 2026-09-28 | 4 | 0 | — |
| [adr/index.md](../adr/index.md) | 2026-09-28 | 1 | 0 | — |
| [claude-tooling.md](../claude-tooling.md) | 2026-09-28 | 46 | 0 | — |
| [index.md](../index.md) | 2026-09-28 | 0 | 0 | — |
| [deploying.md](../deploying.md) | 2026-09-29 | 9 | 0 | — |
| [monitor-bridge-checks.md](../monitor-bridge-checks.md) | 2026-09-29 | 47 | 0 | — |
