# littlelink — the public link-in-bio page

`techno-tim/littlelink-server`, a static link-page server with no config beyond its image.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=littlelink` gates the rollout
- **Image:** `ghcr.io/techno-tim/littlelink-server` (`littlelink_k8s_image`)
- **Route:** `www.<domain>` · `www.local.<domain>`, no Authelia
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it.** Its `containers_list` entry
  is in `cluster/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run.
- **Its rollout.** The Kustomization's `wait` holds the rollout.
  `k8s_autodeploy` gates no apply: Flux applies every merged change. It still
  decides whether Renovate automerges the image bump.
- **Digest-pinned since 2026-08-06** so a k8s rollout re-pull can't silently change what
  runs; the `:latest` tag is kept alongside the digest so Renovate's `k8s-defaults` manager
  still tracks it.
- **Renovate may automerge its image bump because** it is stateless (`RollingUpdate`, no PVC), digest-pinned
  and readinessProbe-backed.

## Editing
- Image bump: `defaults/main.yml` (`littlelink_k8s_image`).
