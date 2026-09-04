---
generated_from: scripts/docs/reference/freshness.py
generated_at: 2026-09-04 20:40 UTC
generated_sha: 9b71a483
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/freshness.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Doc freshness

45 hand-written page(s). *Changed* is the page's last commit; *moved* counts the repo files the page names whose last commit is later than that. A moved source does not prove the page is wrong -- it marks the page to reread next. The generated reference pages are not listed: they are rebuilt from the tree.

| Page | Changed | Sources named | Moved since | Most recently moved |
|---|---|---|---|---|
| [kopia-disaster-recovery.md](../kopia-disaster-recovery.md) | 2026-08-30 | 12 | 9 | `CLAUDE.md` (2026-09-04) |
| [gitops-argo-flux-evaluation.md](../gitops-argo-flux-evaluation.md) | 2026-09-02 | 13 | 8 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-04) |
| [healthchecks-io-deadman.md](../healthchecks-io-deadman.md) | 2026-09-02 | 14 | 7 | `ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2` (2026-09-04) |
| [staging-phase-c.md](../staging-phase-c.md) | 2026-09-03 | 16 | 7 | `ansible/deploy.yml` (2026-09-04) |
| [b2-transaction-cap-monitoring-gaps.md](../b2-transaction-cap-monitoring-gaps.md) | 2026-09-02 | 8 | 5 | `ansible/deploy.yml` (2026-09-04) |
| [longhorn-disaster-recovery.md](../longhorn-disaster-recovery.md) | 2026-09-02 | 13 | 5 | `ansible/deploy.yml` (2026-09-04) |
| [adr/0003-sops-with-age-for-secrets-at-rest.md](../adr/0003-sops-with-age-for-secrets-at-rest.md) | 2026-08-24 | 4 | 3 | `ansible/secret_rotation.yml` (2026-09-04) |
| [anilist-integration.md](../anilist-integration.md) | 2026-09-02 | 3 | 3 | `ansible/roles/k8s/manifests/tasks/main.yml` (2026-09-04) |
| [b2-api-drain-scoping.md](../b2-api-drain-scoping.md) | 2026-09-02 | 4 | 3 | `scripts/secrets_mgmt/secret_rotation.py` (2026-09-04) |
| [longhorn-upgrade.md](../longhorn-upgrade.md) | 2026-09-02 | 7 | 3 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-03) |
| [break-glass.md](../break-glass.md) | 2026-09-03 | 16 | 3 | `docs/reference/secrets.md` (2026-09-04) |
| [gitops-pipeline.md](../gitops-pipeline.md) | 2026-09-03 | 7 | 3 | `ansible/roles/setup/gitops_deploy/CLAUDE.md` (2026-09-04) |
| [networkpolicy-slice-answers.md](../networkpolicy-slice-answers.md) | 2026-09-03 | 18 | 3 | `ansible/roles/setup/fake_remux/tasks/main.yml` (2026-09-04) |
| [staging-cluster.md](../staging-cluster.md) | 2026-09-03 | 31 | 3 | `ansible/tests/staging/test_staging_manifests_have_their_variables.py` (2026-09-04) |
| [adr/0015-d2-for-hand-authored-diagrams.md](../adr/0015-d2-for-hand-authored-diagrams.md) | 2026-09-01 | 2 | 2 | `scripts/docs/build_docs.py` (2026-09-03) |
| [deploying.md](../deploying.md) | 2026-09-01 | 2 | 2 | `scripts/diagnostics/probe.py` (2026-09-04) |
| [adr/0001-mkdocs-site-with-generated-reference.md](../adr/0001-mkdocs-site-with-generated-reference.md) | 2026-09-02 | 4 | 2 | `CLAUDE.md` (2026-09-04) |
| [adr/0010-pull-based-gitops-over-argo-and-flux.md](../adr/0010-pull-based-gitops-over-argo-and-flux.md) | 2026-09-02 | 3 | 2 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-04) |
| [claude-shell-permissions.md](../claude-shell-permissions.md) | 2026-09-02 | 7 | 2 | `CLAUDE.md` (2026-09-04) |
| [networkpolicy-default-deny.md](../networkpolicy-default-deny.md) | 2026-09-02 | 8 | 2 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-03) |
| [longhorn-backup-tiering.md](../longhorn-backup-tiering.md) | 2026-09-03 | 10 | 2 | `scripts/diagnostics/probe.py` (2026-09-04) |
| [secret-rotation.md](../secret-rotation.md) | 2026-09-03 | 4 | 2 | `ansible/secret_rotation.yml` (2026-09-04) |
| [email-to-rss.md](../email-to-rss.md) | 2026-08-15 | 1 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-09-03) |
| [adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | 2026-08-24 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-04) |
| [adr/0005-traefik-is-the-edge-with-ingressroute-crds.md](../adr/0005-traefik-is-the-edge-with-ingressroute-crds.md) | 2026-08-24 | 1 | 1 | `ansible/templates/ingressroute.yml.j2` (2026-09-02) |
| [adr/0007-backup-tiering-r2-daily-b2-weekly.md](../adr/0007-backup-tiering-r2-daily-b2-weekly.md) | 2026-08-24 | 1 | 1 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-03) |
| [adr/0009-networkpolicy-default-deny-ingress.md](../adr/0009-networkpolicy-default-deny-ingress.md) | 2026-08-24 | 1 | 1 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-03) |
| [adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md](../adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | 2026-08-24 | 1 | 1 | `docs/kopia-disaster-recovery.md` (2026-08-30) |
| [adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | 2026-08-25 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-04) |
| [adr/0006-longhorn-for-cluster-storage.md](../adr/0006-longhorn-for-cluster-storage.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-03) |
| [adr/0008-16-mib-longhorn-blocks.md](../adr/0008-16-mib-longhorn-blocks.md) | 2026-09-02 | 1 | 1 | `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` (2026-09-03) |
| [adr/0013-daniel-pi-stays-on-docker.md](../adr/0013-daniel-pi-stays-on-docker.md) | 2026-09-02 | 1 | 1 | `ansible/inventory/host_vars/daniel-pi.yml` (2026-09-03) |
| [adr/index.md](../adr/index.md) | 2026-09-02 | 1 | 1 | `ansible/tests/repo/test_adr_links.py` (2026-09-03) |
| [uptime-robot-monitors.md](../uptime-robot-monitors.md) | 2026-09-02 | 4 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-04) |
| [failure-classes.md](../failure-classes.md) | 2026-09-03 | 15 | 1 | `docs/reference/backlog.md` (2026-09-04) |
| [wireguard-private-homelab-access.md](../wireguard-private-homelab-access.md) | 2026-09-03 | 2 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-04) |
| [adr/0004-authelia-is-the-single-sign-on-layer.md](../adr/0004-authelia-is-the-single-sign-on-layer.md) | 2026-09-02 | 0 | 0 | — |
| [adr/0011-one-lock-serialises-every-deploy-path.md](../adr/0011-one-lock-serialises-every-deploy-path.md) | 2026-09-02 | 3 | 0 | — |
| [security-tools.md](../security-tools.md) | 2026-09-02 | 3 | 0 | — |
| [index.md](../index.md) | 2026-09-03 | 0 | 0 | — |
| [k3s-etcd-restore.md](../k3s-etcd-restore.md) | 2026-09-03 | 6 | 0 | — |
| [adr/0016-code-scanning-stays-on-default-setup.md](../adr/0016-code-scanning-stays-on-default-setup.md) | 2026-09-04 | 8 | 0 | — |
| [claude-tooling.md](../claude-tooling.md) | 2026-09-04 | 19 | 0 | — |
| [post-merge-automation.md](../post-merge-automation.md) | 2026-09-04 | 22 | 0 | — |
| [python-code-organization.md](../python-code-organization.md) | 2026-09-04 | 59 | 0 | — |
