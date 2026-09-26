---
generated_from: scripts/docs/reference/freshness.py
generated_at: 2026-09-26 00:30 UTC
generated_sha: 2d7da5a4b
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/freshness.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Doc freshness

51 hand-written page(s). *Changed* is the page's last commit; *moved* counts the repo files the page names whose last commit is later than that. A moved source does not prove the page is wrong -- it marks the page to reread next. The generated reference pages are not listed: they are rebuilt from the tree.

| Page | Changed | Sources named | Moved since | Most recently moved |
|---|---|---|---|---|
| [python-code-organization.md](../python-code-organization.md) | 2026-09-24 | 66 | 19 | `pyproject.toml` (2026-09-25) |
| [staging-cluster.md](../staging-cluster.md) | 2026-09-23 | 31 | 16 | `ansible/inventory/group_vars/all.yml` (2026-09-25) |
| [break-glass.md](../break-glass.md) | 2026-09-17 | 17 | 13 | `ansible/vars/secrets.yml` (2026-09-25) |
| [deploy-sh-python-port.md](../deploy-sh-python-port.md) | 2026-09-24 | 58 | 13 | `scripts/deploy_tools/deploy_under_locks.py` (2026-09-25) |
| [gitops-argo-flux-evaluation.md](../gitops-argo-flux-evaluation.md) | 2026-09-02 | 13 | 12 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-25) |
| [networkpolicy-slice-answers.md](../networkpolicy-slice-answers.md) | 2026-09-03 | 18 | 11 | `ansible/roles/setup/fake_remux/tasks/main.yml` (2026-09-25) |
| [k3s-upgrade.md](../k3s-upgrade.md) | 2026-09-21 | 13 | 10 | `ansible/k3s-bringup.yml` (2026-09-25) |
| [longhorn-disaster-recovery.md](../longhorn-disaster-recovery.md) | 2026-09-21 | 15 | 10 | `docs/longhorn-backup-tiering.md` (2026-09-26) |
| [b2-transaction-cap-monitoring-gaps.md](../b2-transaction-cap-monitoring-gaps.md) | 2026-09-02 | 8 | 8 | `docs/longhorn-backup-tiering.md` (2026-09-26) |
| [secret-rotation.md](../secret-rotation.md) | 2026-09-21 | 12 | 6 | `ansible/vars/secrets.yml` (2026-09-25) |
| [claude-tooling.md](../claude-tooling.md) | 2026-09-24 | 42 | 6 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-09-26) |
| [issue-claiming-and-fanout.md](../issue-claiming-and-fanout.md) | 2026-09-21 | 20 | 5 | `scripts/dev/prune_worktrees.py` (2026-09-25) |
| [self-hosted-runner-spike.md](../self-hosted-runner-spike.md) | 2026-09-22 | 7 | 5 | `pyproject.toml` (2026-09-25) |
| [adr/0003-sops-with-age-for-secrets-at-rest.md](../adr/0003-sops-with-age-for-secrets-at-rest.md) | 2026-08-24 | 4 | 4 | `ansible/vars/secrets.yml` (2026-09-25) |
| [adr/0001-mkdocs-site-with-generated-reference.md](../adr/0001-mkdocs-site-with-generated-reference.md) | 2026-09-02 | 4 | 4 | `scripts/docs/service_catalog.py` (2026-09-24) |
| [adr/0016-code-scanning-stays-on-default-setup.md](../adr/0016-code-scanning-stays-on-default-setup.md) | 2026-09-17 | 9 | 4 | `scripts/deploy_tools/land_tags.py` (2026-09-25) |
| [longhorn-upgrade.md](../longhorn-upgrade.md) | 2026-09-21 | 9 | 4 | `ansible/k3s-bringup.yml` (2026-09-25) |
| [k3s-etcd-restore.md](../k3s-etcd-restore.md) | 2026-09-24 | 9 | 4 | `ansible/inventory/group_vars/all.yml` (2026-09-25) |
| [kopia-disaster-recovery.md](../kopia-disaster-recovery.md) | 2026-09-24 | 10 | 4 | `ansible/bootstrap.yml` (2026-09-25) |
| [staging-phase-c.md](../staging-phase-c.md) | 2026-09-24 | 16 | 4 | `ansible/initial_setup.yml` (2026-09-25) |
| [anilist-integration.md](../anilist-integration.md) | 2026-09-06 | 3 | 3 | `ansible/roles/k8s/manifests/tasks/main.yml` (2026-09-25) |
| [adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md](../adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | 2026-09-09 | 3 | 3 | `scripts/secrets_mgmt/git_dates.py` (2026-09-24) |
| [adr/0011-one-lock-serialises-every-deploy-path.md](../adr/0011-one-lock-serialises-every-deploy-path.md) | 2026-09-12 | 3 | 3 | `ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py` (2026-09-25) |
| [adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md](../adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) | 2026-09-24 | 5 | 3 | `scripts/dev/prune_worktrees.py` (2026-09-25) |
| [adr/0015-d2-for-hand-authored-diagrams.md](../adr/0015-d2-for-hand-authored-diagrams.md) | 2026-09-01 | 2 | 2 | `scripts/docs/build_docs.py` (2026-09-24) |
| [adr/0010-pull-based-gitops-over-argo-and-flux.md](../adr/0010-pull-based-gitops-over-argo-and-flux.md) | 2026-09-02 | 3 | 2 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-25) |
| [networkpolicy-default-deny.md](../networkpolicy-default-deny.md) | 2026-09-18 | 11 | 2 | `ansible/roles/k8s/sonarr/tasks/main.yml` (2026-09-24) |
| [uptime-robot-monitors.md](../uptime-robot-monitors.md) | 2026-09-21 | 5 | 2 | `docs/kopia-disaster-recovery.md` (2026-09-24) |
| [adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | 2026-08-24 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-24) |
| [adr/0005-traefik-is-the-edge-with-ingressroute-crds.md](../adr/0005-traefik-is-the-edge-with-ingressroute-crds.md) | 2026-08-24 | 1 | 1 | `ansible/templates/ingressroute.yml.j2` (2026-09-18) |
| [adr/0007-backup-tiering-r2-daily-b2-weekly.md](../adr/0007-backup-tiering-r2-daily-b2-weekly.md) | 2026-08-24 | 1 | 1 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-24) |
| [adr/0009-networkpolicy-default-deny-ingress.md](../adr/0009-networkpolicy-default-deny-ingress.md) | 2026-08-24 | 1 | 1 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-22) |
| [adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | 2026-08-25 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-24) |
| [adr/0006-longhorn-for-cluster-storage.md](../adr/0006-longhorn-for-cluster-storage.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-05) |
| [adr/0008-16-mib-longhorn-blocks.md](../adr/0008-16-mib-longhorn-blocks.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-05) |
| [adr/0013-daniel-pi-stays-on-docker.md](../adr/0013-daniel-pi-stays-on-docker.md) | 2026-09-02 | 1 | 1 | `ansible/inventory/host_vars/daniel-pi.yml` (2026-09-24) |
| [security-tools.md](../security-tools.md) | 2026-09-02 | 3 | 1 | `ansible/initial_setup.yml` (2026-09-25) |
| [wireguard-private-homelab-access.md](../wireguard-private-homelab-access.md) | 2026-09-03 | 2 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-22) |
| [email-to-rss.md](../email-to-rss.md) | 2026-09-21 | 1 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-09-26) |
| [claude-shell-permissions.md](../claude-shell-permissions.md) | 2026-09-24 | 7 | 1 | `ansible/vars/secrets.yml` (2026-09-25) |
| [failure-classes.md](../failure-classes.md) | 2026-09-24 | 15 | 1 | `docs/reference/backlog.md` (2026-09-25) |
| [adr/0004-authelia-is-the-single-sign-on-layer.md](../adr/0004-authelia-is-the-single-sign-on-layer.md) | 2026-09-02 | 0 | 0 | — |
| [index.md](../index.md) | 2026-09-03 | 0 | 0 | — |
| [adr/index.md](../adr/index.md) | 2026-09-12 | 1 | 0 | — |
| [deploying.md](../deploying.md) | 2026-09-25 | 9 | 0 | — |
| [gitops-pipeline.md](../gitops-pipeline.md) | 2026-09-25 | 73 | 0 | — |
| [healthchecks-io-deadman.md](../healthchecks-io-deadman.md) | 2026-09-25 | 17 | 0 | — |
| [monitor-bridge-checks.md](../monitor-bridge-checks.md) | 2026-09-25 | 47 | 0 | — |
| [post-merge-automation.md](../post-merge-automation.md) | 2026-09-25 | 30 | 0 | — |
| [b2-api-drain-scoping.md](../b2-api-drain-scoping.md) | 2026-09-26 | 5 | 0 | — |
| [longhorn-backup-tiering.md](../longhorn-backup-tiering.md) | 2026-09-26 | 10 | 0 | — |
