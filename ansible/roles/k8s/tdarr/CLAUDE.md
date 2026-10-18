# tdarr — hardware-accelerated media transcoding

Tdarr, transcoding through the node's Intel VAAPI device (`tdarr_k8s_dri_resource:
devic.es/dri`, advertised by `k8s/dri-device-plugin`). See repo-root `CLAUDE.md` for
shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=tdarr`
  with exit 5
- **Image:** `ghcr.io/haveagitgat/tdarr` (`tdarr_k8s_image`)
- **Route:** `tdarr.<domain>` · `tdarr.local.<domain>`, Authelia one_factor
- **Claims:** `tdarr-server` (weekly -> B2 (default target)), `tdarr-configs` (weekly -> B2
  (default target)), `media-data` (not Longhorn (media-local))
<!-- /generated_from -->

- **`tdarr-server`** (3Gi, `longhorn`, backed up — the 204MB library/transcode
  DB) and `tdarr-configs` (1Gi, `longhorn` — flow/plugin definitions), kept separate so
  the server DB's churn doesn't drag `logs/`/`transcode_cache/` onto Longhorn. Also
  mounts the shared `media-data` RWX volume, **read-write** — the one media consumer
  that rewrites library files in place.
- **The snapshot/revert machinery works here, but a revert can't undo a completed transcode**: it rewrites the untranscoded original on
  `media-data`, which is not reverted. Two claims also mean a failed deploy can revert
  them to different points in time. Full reasoning in `defaults/main.yml`.

## Notable
- **The image pin carries a version tag, so a major release reads as one.** Upstream tags
  every release on ghcr.io, and the `:latest` digest pin it replaced hid Tdarr v3 inside a
  bare digest bump (#4555). A version bump is still a manual landing: the denylist keeps
  Renovate's automerge off.
- **The Deployment pins the engine to Rust and Tdarr's own auth off.** A 2.x server picks its
  engine from upstream on every start. The Rust engine migrated the library DB out of the
  NodeJS engine's `DB2/` tree, so NodeJS would read state from before the move. The engine
  pin is what makes a rollback to a 2.x tag safe.
  Tdarr's auth stays off because Authelia is the login; 3.x would otherwise turn it on for a
  fresh `tdarr-configs` claim. The reasoning sits beside both env entries in
  `templates/deployment.yaml.j2`.
- `tdarr_k8s_server_port` (8266, the internal-node control channel) is deliberately
  **not** in the Service — exposing it would publish an unauthenticated channel for a
  process that rewrites media.
- **Flux owns tdarr (D5, Wave D).** `promote` applies it, and no Ansible run reaches
  `tasks/main.yml`. Its checks run in three places, each on every roll:
    - `tdarr-prepull` pulls the image onto the node before `tdarr` applies;
    - the Deployment's `vaapi-encode` initContainer runs the encode;
    - the `tdarr-verify` Job proves the render node opens and the library is writable.
- **`templates/verify/` is the `tdarr-verify` Job.** The renderer emits it once the `tdarr`
  Kustomization reads Ready. The Job runs `files/tdarr_verify.py`, which replaced the two exec
  checks of the Ansible verify tasks: the render node opens, and the library is writable. Its
  pod is built like tdarr's, with the same device request, render group, PUID and `media-data`
  subPath, so it checks that wiring and not the live pod. Its spec embeds a digest of tdarr's
  rendered Deployment, so every change that rolls tdarr renames the Job and Flux runs it again.
  The VAAPI encode through tdarr's own ffmpeg has no Job form, because the Job image has no
  ffmpeg and the Job census forbids spawning a process and opening a socket.
  `tests/test_tdarr_verify.py` renders the role and runs the Job census over the Job.
- **The VAAPI encode is the Deployment's `vaapi-encode` initContainer (#5451, option 1).** It
  runs tdarr's own ffmpeg in tdarr's image, as tdarr's PUID, with the same device request, so
  a broken driver or ffmpeg build keeps tdarr from starting. A failed encode holds the pod in
  Init, so the `tdarr` Kustomization fails its health check at the timeout; read the init
  container's log for the ffmpeg error.
- **The rollout budget is the 600s default.** The literal
  `ansible/roles/k8s/tdarr/defaults/main.yml:manifests_rollout_timeout` is Flux's `timeout`,
  and the template's `progressDeadlineSeconds` reads it through `tdarr_k8s_rollout_timeout`,
  so the deadline moves with the wait. It was 3000s until the flip, because a cold pull of the
  1432 MiB image took 25m37s on 2026-10-08 (#3689) and Ansible's pre-pull shared the budget.
- **`tdarr-prepull` pulls the image before the Recreate (#3875).** `Recreate` stops the old pod
  first, so without the pre-pull tdarr is down for the whole 25-minute pull. The renderer adds
  the Kustomization because `ansible/roles/k8s/tdarr/defaults/main.yml:flux_prepull_timeout`
  is set (`scripts/renderer/renderer_lib/prepull.py`), and `tdarr` depends on it. Its Job
  copies the Deployment's placement. That is why the Deployment carries a required node
  affinity to `inventory_hostname`, the node the `media-data` PV already pins it to: the
  renderer refuses a pre-pull for a workload that names no node.
  `ansible/tests/services/test_tdarr_prepulls_its_image.py` holds both halves.
