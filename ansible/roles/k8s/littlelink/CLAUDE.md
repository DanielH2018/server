# littlelink — the public link-in-bio page

`techno-tim/littlelink-server`, a static link-page server with no config beyond its image.

## At a glance
- **Image:** `ghcr.io/techno-tim/littlelink-server:latest@sha256:...` — digest-pinned since
  2026-08-06 so a k8s rollout re-pull can't silently change what runs; the `:latest` tag is
  kept alongside the digest so Renovate's `k8s-defaults` manager still tracks it.
- **Deploy tag:** `--tags "littlelink"`.
- **Route:** `www.<domain>` — no Authelia, public.
- **Persists:** nothing — stateless, no PVC, `RollingUpdate` strategy.
- **Auto-deploy:** eligible — stateless, digest-pinned, readinessProbe-backed.

## Editing
- Image bump: `defaults/main.yml` (`littlelink_k8s_image`).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "littlelink"`.
