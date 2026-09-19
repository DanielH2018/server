# peanut — UPS web dashboard (PeaNUT)

Web UI for the NUT UPS daemon. Reads `upsd` over the in-cluster `nut` Service — it owns no
UPS state of its own; the physical UPS is USB-attached to daniel-server via the `nut` role.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "peanut"`
- **Images:** `brandawg93/peanut` (`peanut_k8s_image`), `alpine` (`peanut_k8s_init_image`)
- **Route:** `peanut.<domain>` · `peanut.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Pinned `tag@sha256`** — same tag the Docker-era role ran, kept in lockstep so a behavior
  difference between the two is never the image.
- **Port:** 8080
- **No PVC** — `/config` and `/app/config` are `emptyDir`, seeded at boot.
- **Auto-deploy-eligible because** it is a stateless RollingUpdate with a readinessProbe and
  a digest-pinned image.
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
