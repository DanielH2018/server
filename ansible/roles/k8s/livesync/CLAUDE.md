# livesync — CouchDB for Obsidian LiveSync

CouchDB backend for the Obsidian Self-hosted LiveSync plugin. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `couchdb:3.5.2` (pinned + Renovate-managed, watchtower opts out)
- **Host: daniel-box (k8s), since 2026-08-06 — slice 2.** The Docker role this config came from
  is gone; `local.ini.j2` now lives in this role's `templates/`, rendered into the ConfigMap.
  Edit CouchDB config HERE; deploy with `--tags livesync` from daniel-box.
- **Port:** 5984 · **URL:** `livesync.<domain>` (forwards to the cluster via `bridge_hostname`)
- **Authelia:** **no** — CouchDB enforces its own auth (`require_valid_user = true`);
  the LiveSync client uses basic auth and can't pass Authelia 2FA
- **Networks:** apps
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Notable
- `templates/local.ini.j2` sets `require_valid_user` and smoosh auto-compaction ratios
  (curbing `.couch` bloat from Obsidian LiveSync's MVCC revisions). Admin creds come from
  `ansible/vars/secrets.yml`.

## Editing
- CouchDB cfg: `templates/local.ini.j2` (rendered into the k8s ConfigMap by `roles/k8s/livesync`)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "livesync"`
- `templates/docker-compose.yml.j2` is a frozen rollback artifact — it no longer deploys and
  Renovate ignores it.
