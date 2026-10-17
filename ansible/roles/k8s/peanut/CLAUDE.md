# peanut — UPS web dashboard (PeaNUT)

Web UI for the NUT UPS daemon. Reads `upsd` over the in-cluster `nut` Service — it owns no
UPS state of its own; the physical UPS is USB-attached to daniel-server via the `nut` role.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=peanut`
  with exit 5
- **Images:** `brandawg93/peanut` (`peanut_k8s_image`), `alpine` (`peanut_k8s_init_image`)
- **Route:** `peanut.<domain>` · `peanut.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Pinned `tag@sha256`** — same tag the Docker-era role ran, kept in lockstep so a behavior
  difference between the two is never the image.
- **Port:** 8080
- **No PVC** — `/config` and `/app/config` are `emptyDir`, seeded at boot.
- **Flux applies it, not Ansible** (D3.6, the first Flux role with a Secret). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. Its digest-bump PRs therefore draw no `land-wait` and no
  `probe.py health` run. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
- **Secrets:** `peanut_username`, `peanut_password` (web UI login), `nut_ha_password`
  (upsd's read-only login, shared with Home Assistant, which PeaNUT uses to poll `upsd`) —
  see `templates/secret.yaml.j2`. The published render holds a `${<key>}` placeholder for
  each, and Flux substitutes the values from the `homelab-cluster-vars` Secret, which it
  decrypts from `ansible/vars/cluster-vars.sops.env`. The Kustomization depends on
  `cluster-vars`, so a rotated value is live before peanut reconciles.
- **A changed value rolls the pod.** No template here carries the annotation that does it:
  the renderer stamps a restart checksum on the Deployment of every flux-owned role, a hash
  of the ciphertext, never of a value (`docs/flux-deploy-plane.md`).
- **Rotating `peanut_password` or `peanut_username`** is operator-only at the source: `sops
  set`, then `cluster_vars.py write`, committed together. homepage's PeaNUT widget renders
  the same login. homepage has been flux-owned since D5.7 and substitutes it from the same
  `homelab-cluster-vars` Secret, so the next promotion carries the rotation to both roles,
  and no `deploy.sh` run follows. `secret_rotation.py consumers peanut_password` prints both
  halves.

## Notable
- **`alpine` init container, not the app image**, seeds `/config/settings.yml` from the
  Secret — PeaNUT's own image has no `cp` binary (`StartError` at first deploy, 2026-08-12).
- **`fsGroup: 1000`, no `runAsUser` pin.** The image's own non-root `node` user serves the
  page, so pinning a UID here would be an unrelated behavior change. `fsGroup` makes the
  `/app/config` `emptyDir` writable as a supplementary group regardless of which UID the image
  runs as.
- **`auth.yaml` is regenerated every boot** from `WEB_USERNAME`/`WEB_PASSWORD` into the
  `peanut-app-config` `emptyDir` — it's absent from the image, not part of the seeded mount.

## Editing
- Manifest: `templates/deployment.yaml.j2` · Secret: `templates/secret.yaml.j2`
