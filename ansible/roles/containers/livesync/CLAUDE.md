# livesync — CouchDB for Obsidian LiveSync

CouchDB backend for the Obsidian Self-hosted LiveSync plugin. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `couchdb:3`
- **Host:** daniel-server · **Port:** 5984 · **URL:** `livesync.<domain>`
- **Authelia:** **no** — CouchDB enforces its own auth (`require_valid_user = true`);
  the LiveSync client uses basic auth and can't pass Authelia 2FA
- **Networks:** apps
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `templates/local.ini.j2` sets `require_valid_user`, CORS, and chunk-size tuning needed
  by Obsidian LiveSync. Admin creds come from `ansible/vars/secrets.yml`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · CouchDB cfg: `templates/local.ini.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "livesync"`
