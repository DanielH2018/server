# littlelink — the public link-in-bio page

`techno-tim/littlelink-server`, a static link-page server with no config beyond its image.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "littlelink"`
- **Image:** `ghcr.io/techno-tim/littlelink-server` (`littlelink_k8s_image`)
- **Route:** `www.<domain>` · `www.local.<domain>`, no Authelia
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Digest-pinned since 2026-08-06** so a k8s rollout re-pull can't silently change what
  runs; the `:latest` tag is kept alongside the digest so Renovate's `k8s-defaults` manager
  still tracks it.
- **Eligible for auto-deploy because** it is stateless (`RollingUpdate`, no PVC), digest-pinned
  and readinessProbe-backed.

## Editing
- Image bump: `defaults/main.yml` (`littlelink_k8s_image`).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "littlelink"`.
