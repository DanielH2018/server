# littlelink — the public link-in-bio page

`techno-tim/littlelink-server`, a static link-page server with no config beyond its image.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=littlelink` with exit 5
- **Image:** `ghcr.io/techno-tim/littlelink-server` (`littlelink_k8s_image`)
- **Route:** `www.<domain>` · `www.local.<domain>`, no Authelia
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.6, the second Wave A PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Flux applies the same render, and the
  Kustomization's `wait` replaces the rollout wait.
  `k8s_autodeploy` no longer gates an apply, because the deployer skips the tag. It still
  decides whether Renovate automerges the image bump.
- **The flip waited on M6's health-gate drill.** That drill needed an Ansible-owned tag with
  a release record, and littlelink was the one M6 named. M6's B-1e removed the drill rule on
  2026-10-17, so the flip no longer waits (`docs/target-architecture/monitoring/m6-retire-kuma.md`,
  Open question 12).
- **Digest-pinned since 2026-08-06** so a k8s rollout re-pull can't silently change what
  runs; the `:latest` tag is kept alongside the digest so Renovate's `k8s-defaults` manager
  still tracks it.
- **Renovate may automerge its image bump because** it is stateless (`RollingUpdate`, no PVC), digest-pinned
  and readinessProbe-backed.

## Editing
- Image bump: `defaults/main.yml` (`littlelink_k8s_image`).
