# tdarr — hardware-accelerated media transcoding

Tdarr, transcoding through the node's Intel VAAPI device (`tdarr_k8s_dri_resource:
devic.es/dri`, advertised by `k8s/dri-device-plugin`). See repo-root `CLAUDE.md` for
shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "tdarr"`
- **Image:** `ghcr.io/haveagitgat/tdarr` (`tdarr_k8s_image`)
- **Route:** `tdarr.<domain>` · `tdarr.local.<domain>`, Authelia one_factor
- **Claims:** `tdarr-server` (weekly -> B2 (default target)), `tdarr-configs` (weekly -> B2
  (default target)), `media-data` (not Longhorn (media-local))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — state coupled outside the volume —
  rewrites media-data (shared RWX, not reverted) in place, an irreversible transcode the
  snapshot/revert can't undo; ALSO non-atomic two-claim revert (tdarr-configs, tdarr-server);
  ALSO the digest pin's 'stays manual' intent is unenforced — renovate.json automerges a digest
  re-push after a 3-day soak with no tdarr exclusion
<!-- /generated_from -->

- **`tdarr-server`** (3Gi, `longhorn`, backed up — the 204MB library/transcode
  DB) and `tdarr-configs` (1Gi, `longhorn` — flow/plugin definitions), kept separate so
  the server DB's churn doesn't drag `logs/`/`transcode_cache/` onto Longhorn. Also
  mounts the shared `media-data` RWX volume, **read-write** — the one media consumer
  that rewrites library files in place.
- **The snapshot/revert machinery works here, but a revert can't undo a completed transcode**: it rewrites the untranscoded original on
  `media-data`, which is not reverted. Two claims also mean a failed deploy can revert
  them to different points in time, and the digest pin's "stays manual" intent has no
  enforcement against a Renovate digest re-push. Full reasoning in `defaults/main.yml`.

## Notable
- `tdarr_k8s_server_port` (8266, the internal-node control channel) is deliberately
  **not** in the Service — exposing it would publish an unauthenticated channel for a
  process that rewrites media.
- `tasks/verify.yml` gates on `rollout status`, not a readiness wait: every Deployment
  here is single-replica with `maxUnavailable: 0`, so the OLD pod satisfies readiness
  through the whole rollout and a readiness-based check would silently verify the pod
  being replaced.
