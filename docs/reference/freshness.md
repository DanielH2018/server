---
generated_from: scripts/docs/gen_reference_freshness.py
generated_at: 2026-09-02 12:59 UTC
generated_sha: 34840fd7
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/gen_reference_freshness.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Doc freshness

41 hand-written page(s). *Changed* is the page's last commit; *moved* counts the repo files the page names whose last commit is later than that. A moved source does not prove the page is wrong -- it marks the page to reread next. The generated reference pages are not listed: they are rebuilt from the tree.

| Page | Changed | Sources named | Moved since | Most recently moved |
|---|---|---|---|---|
| [b2-transaction-cap-monitoring-gaps.md](../b2-transaction-cap-monitoring-gaps.md) | 2026-08-24 | 8 | 8 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-02) |
| [networkpolicy-slice-answers.md](../networkpolicy-slice-answers.md) | 2026-09-01 | 18 | 7 | `ansible/roles/setup/k3s/tasks/agent.yml` (2026-09-02) |
| [kopia-disaster-recovery.md](../kopia-disaster-recovery.md) | 2026-08-30 | 12 | 6 | `docs/longhorn-disaster-recovery.md` (2026-09-02) |
| [gitops-argo-flux-evaluation.md](../gitops-argo-flux-evaluation.md) | 2026-09-01 | 13 | 5 | `scripts/validate/validate_k8s_manifests.py` (2026-09-02) |
| [adr/0001-mkdocs-site-with-generated-reference.md](../adr/0001-mkdocs-site-with-generated-reference.md) | 2026-08-25 | 4 | 4 | `scripts/docs/service_catalog.py` (2026-09-02) |
| [k3s-etcd-restore.md](../k3s-etcd-restore.md) | 2026-08-31 | 6 | 4 | `ansible/k3s-bringup.yml` (2026-09-02) |
| [b2-api-drain-scoping.md](../b2-api-drain-scoping.md) | 2026-08-22 | 4 | 3 | `scripts/secrets_mgmt/secret_rotation.py` (2026-09-02) |
| [adr/0003-sops-with-age-for-secrets-at-rest.md](../adr/0003-sops-with-age-for-secrets-at-rest.md) | 2026-08-24 | 4 | 3 | `ansible/.sops.yaml` (2026-09-02) |
| [adr/0010-pull-based-gitops-over-argo-and-flux.md](../adr/0010-pull-based-gitops-over-argo-and-flux.md) | 2026-08-25 | 3 | 3 | `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` (2026-09-02) |
| [longhorn-backup-tiering.md](../longhorn-backup-tiering.md) | 2026-08-25 | 5 | 3 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-02) |
| [uptime-robot-monitors.md](../uptime-robot-monitors.md) | 2026-08-30 | 4 | 3 | `docs/longhorn-disaster-recovery.md` (2026-09-02) |
| [email-to-rss.md](../email-to-rss.md) | 2026-08-15 | 1 | 1 | `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` (2026-09-01) |
| [adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md](../adr/0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | 2026-08-24 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-02) |
| [adr/0005-traefik-is-the-edge-with-ingressroute-crds.md](../adr/0005-traefik-is-the-edge-with-ingressroute-crds.md) | 2026-08-24 | 1 | 1 | `ansible/templates/ingressroute.yml.j2` (2026-09-02) |
| [adr/0007-backup-tiering-r2-daily-b2-weekly.md](../adr/0007-backup-tiering-r2-daily-b2-weekly.md) | 2026-08-24 | 1 | 1 | `ansible/roles/setup/k3s/defaults/main.yml` (2026-09-02) |
| [adr/0009-networkpolicy-default-deny-ingress.md](../adr/0009-networkpolicy-default-deny-ingress.md) | 2026-08-24 | 1 | 1 | `ansible/roles/k8s/netpol-baseline/defaults/main.yml` (2026-09-02) |
| [adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md](../adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | 2026-08-24 | 1 | 1 | `docs/kopia-disaster-recovery.md` (2026-08-30) |
| [security-tools.md](../security-tools.md) | 2026-08-24 | 3 | 1 | `ansible/initial_setup.yml` (2026-08-29) |
| [wireguard-private-homelab-access.md](../wireguard-private-homelab-access.md) | 2026-08-24 | 2 | 1 | `ansible/inventory/host_vars/daniel-box.yml` (2026-09-02) |
| [adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md](../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | 2026-08-25 | 1 | 1 | `scripts/diagnostics/probe.py` (2026-09-02) |
| [adr/0015-d2-for-hand-authored-diagrams.md](../adr/0015-d2-for-hand-authored-diagrams.md) | 2026-09-01 | 2 | 1 | `scripts/docs/build_docs.py` (2026-09-02) |
| [deploying.md](../deploying.md) | 2026-09-01 | 2 | 1 | `scripts/diagnostics/probe.py` (2026-09-02) |
| [adr/0004-authelia-is-the-single-sign-on-layer.md](../adr/0004-authelia-is-the-single-sign-on-layer.md) | 2026-08-24 | 0 | 0 | — |
| [adr/0006-longhorn-for-cluster-storage.md](../adr/0006-longhorn-for-cluster-storage.md) | 2026-09-02 | 1 | 0 | — |
| [adr/0008-16-mib-longhorn-blocks.md](../adr/0008-16-mib-longhorn-blocks.md) | 2026-09-02 | 1 | 0 | — |
| [adr/0011-one-lock-serialises-every-deploy-path.md](../adr/0011-one-lock-serialises-every-deploy-path.md) | 2026-09-02 | 3 | 0 | — |
| [adr/0013-daniel-pi-stays-on-docker.md](../adr/0013-daniel-pi-stays-on-docker.md) | 2026-09-02 | 1 | 0 | — |
| [adr/index.md](../adr/index.md) | 2026-09-02 | 1 | 0 | — |
| [anilist-integration.md](../anilist-integration.md) | 2026-09-02 | 3 | 0 | — |
| [claude-shell-permissions.md](../claude-shell-permissions.md) | 2026-09-02 | 7 | 0 | — |
| [claude-tooling.md](../claude-tooling.md) | 2026-09-02 | 12 | 0 | — |
| [gitops-pipeline.md](../gitops-pipeline.md) | 2026-09-02 | 6 | 0 | — |
| [healthchecks-io-deadman.md](../healthchecks-io-deadman.md) | 2026-09-02 | 14 | 0 | — |
| [index.md](../index.md) | 2026-09-02 | 0 | 0 | — |
| [longhorn-disaster-recovery.md](../longhorn-disaster-recovery.md) | 2026-09-02 | 13 | 0 | — |
| [longhorn-upgrade.md](../longhorn-upgrade.md) | 2026-09-02 | 7 | 0 | — |
| [networkpolicy-default-deny.md](../networkpolicy-default-deny.md) | 2026-09-02 | 8 | 0 | — |
| [post-merge-automation.md](../post-merge-automation.md) | 2026-09-02 | 21 | 0 | — |
| [secret-rotation.md](../secret-rotation.md) | 2026-09-02 | 4 | 0 | — |
| [staging-cluster.md](../staging-cluster.md) | 2026-09-02 | 26 | 0 | — |
| [staging-phase-c.md](../staging-phase-c.md) | 2026-09-02 | 13 | 0 | — |
