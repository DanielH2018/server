# livesync — CouchDB for Obsidian LiveSync

CouchDB backend for the Obsidian Self-hosted LiveSync plugin. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=livesync` gates the rollout
- **Image:** `couchdb` (`livesync_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `livesync-data` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **Host: daniel-box (k8s), since 2026-08-06 — slice 2.** The Docker role this config came from
  is gone; `local.ini.j2` now lives in this role's `templates/`, rendered into the ConfigMap.
  Edit CouchDB config HERE, then merge; `promote` applies it.
- **Port:** 5984
- **No Authelia because** CouchDB enforces its own auth (`require_valid_user = true`); the
  LiveSync client uses basic auth and can't pass Authelia 2FA
- **Depends on:** traefik
- **Flux applies it, not Ansible** (D5.9, the first Wave B PR). Its `containers_list` entry is
  in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster when
  `promote` publishes and verifies the next bundle. traefik is flux-owned too, so the
  Kustomization lists it in `dependsOn`, and Flux applies this tag only after its Kustomization is
  Ready. The Deployment is `Recreate`, so a change that rolls it takes sync down briefly.
- **`k8s_autodeploy: false` still stops Renovate automerging the image bump.** The deployer
  skips the tag, so the flag no longer gates an apply. The denial reason above is about the
  revert, not the snapshot.
- **The `livesync-snapshot` Job snapshots `livesync-data` before each revision applies**
  (D5.2a). The livesync Kustomization depends on `livesync-snapshot`, so a revision applies
  only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. The
  claim is `longhorn-nobackup`, so the snapshot is Longhorn-local and no backup copies it. A
  failed revision that changes livesync writes the stateful hold, and `promote` does not reset
  `prod`. A volume revert is not the default answer to that hold here: reverting the B-tree
  desynchronises it from the clients' already-synced revisions, which is the denial reason
  above. Weigh a forward fix before the revert runbook in `docs/volume-snapshot-drills.md`.
- **The CouchDB credentials Secret substitutes from `homelab-cluster-vars`.**
  `livesync_db_password` is a `${<key>}` placeholder in the published render. Flux substitutes
  the value from the `homelab-cluster-vars` Secret, which it decrypts from
  `ansible/vars/cluster-vars.sops.env`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before livesync reconciles. A rotation reaches the cluster only through
  `cluster_vars.py write`, which the operator runs after a hand rotation and the rotation cron
  runs after it rotates an `auto`-tier key. The renderer stamps a restart checksum on the
  Deployment, so a changed value or a `local.ini` edit rolls the pod.
- **Where the task went.** Its pre-apply snapshot through
  `k8s/volume-snapshot` became the `livesync-snapshot` Job, and the Kustomization's `wait`
  replaces the rollout wait. The renderer reads the role's `manifests_exclude_files:
  [ingressroute.yaml]`, so the published render holds no IngressRoute, and traefik's
  file-provider gate stays the only router for the livesync names.

## Notable
- `templates/config/local.ini.j2` sets `require_valid_user` and smoosh auto-compaction ratios
  (curbing `.couch` bloat from Obsidian LiveSync's MVCC revisions). Admin creds come from
  `ansible/vars/secrets.yml`.
- **Smoosh and `_compact` reclaim only MVCC revision bloat, not live chunk growth.** Compare
  `sizes.file` with `sizes.active` in the database info document (`GET /<db>`): compaction helps
  only when `sizes.file` is well above `sizes.active`. When the two are close, the growth is
  LiveSync's live content-addressed chunks, which the plugin's own history still references. The
  lever for those is client-side, the plugin's chunk garbage collection or "Rebuild everything".

## Editing
- CouchDB cfg: `templates/config/local.ini.j2` (rendered into the k8s ConfigMap by `roles/k8s/livesync`)
