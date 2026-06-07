# kopia — Encrypted backup client

Kopia backup server/UI for encrypted, deduplicated backups. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `kopia/kopia` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **Port:** 51515 · **URL:** `kopia.<domain>` (Authelia: yes)
- **Networks:** `kopia` — a dedicated isolation net shared only with Traefik
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Runs intentionally unauthenticated** (workaround for a Kopia basic-auth bug). This is
  by design — don't re-flag it as a vuln. Authelia in front + the dedicated `kopia`
  network (no other app container can reach `kopia:51515`) are the compensating controls.
- `templates/entrypoint.sh.j2` starts the server; `templates/kopiaignore.j2` is the
  global exclude list.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Entry/ignore: `templates/entrypoint.sh.j2`, `kopiaignore.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "kopia"`
