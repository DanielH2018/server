# livesync — CouchDB for Obsidian LiveSync

CouchDB backend for the Obsidian Self-hosted LiveSync plugin. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "livesync"`
- **Image:** `couchdb` (`livesync_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `livesync-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — state coupled outside the volume —
  reverting the CouchDB B-tree to a snapshot desynchronises it from connected Obsidian clients'
  already-synced revisions, inviting a conflict storm an un-reverted rollback would not cause;
  the pre-apply snapshot and revert work fine and are not the blocker
<!-- /generated_from -->

- **Host: daniel-box (k8s), since 2026-08-06 — slice 2.** The Docker role this config came from
  is gone; `local.ini.j2` now lives in this role's `templates/`, rendered into the ConfigMap.
  Edit CouchDB config HERE; deploy with `--tags livesync` from daniel-box.
- **Port:** 5984
- **No Authelia because** CouchDB enforces its own auth (`require_valid_user = true`); the
  LiveSync client uses basic auth and can't pass Authelia 2FA
- **Depends on:** traefik

## Notable
- `templates/config/local.ini.j2` sets `require_valid_user` and smoosh auto-compaction ratios
  (curbing `.couch` bloat from Obsidian LiveSync's MVCC revisions). Admin creds come from
  `ansible/vars/secrets.yml`.

## Editing
- CouchDB cfg: `templates/config/local.ini.j2` (rendered into the k8s ConfigMap by `roles/k8s/livesync`)
- Deploy (from daniel-box): `./scripts/deploy.sh --tags "livesync"`
