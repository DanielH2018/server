# peanut — UPS web dashboard (PeaNUT)

Web UI for the NUT UPS daemon. Reads `upsd` over the in-cluster `nut` Service — it owns no
UPS state of its own; the physical UPS is USB-attached to daniel-server via the `nut` role.

## At a glance
- **Image:** `peanut_k8s_image`, `tag@sha256` pinned — same tag the Docker-era role ran, kept
  in lockstep so a behavior difference between the two is never the image.
- **Route:** `peanut.<domain>` · Authelia · port 8080
- **No PVC** — `/config` and `/app/config` are `emptyDir`, seeded at boot.
- **Deploy tag:** `--tags "peanut"`. `k8s_autodeploy: true` — stateless RollingUpdate, no PVC,
  readinessProbe, digest-pinned image.
- **Secrets:** `peanut_username`, `peanut_password` (web UI login), `nut_monitor_password`
  (upsmon credential PeaNUT uses to poll `upsd`) — see `templates/secret.yaml.j2`.

## Notable
- **`alpine` init container, not the app image**, seeds `/config/settings.yml` from the
  Secret — PeaNUT's own image has no `cp` binary (`StartError` at first deploy, 2026-08-12).
- **`fsGroup: 1000`, no `runAsUser` pin.** The compose template never set a `user:` either, so
  pinning a UID here would be a silent behavior change; `fsGroup` makes `/app/config` writable
  as a supplementary group regardless of which UID the image runs as.
- **`auth.yaml` is regenerated every boot** from `WEB_USERNAME`/`WEB_PASSWORD` into the
  `peanut-app-config` `emptyDir` — it's absent from the image, not part of the seeded mount.

## Editing
- Manifest: `templates/deployment.yaml.j2` · Secret: `templates/secret.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "peanut"`
