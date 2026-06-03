# authelia — SSO / forward-auth middleware

Provides the authentication layer used by every service with `use_authelia: true`.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `authelia/authelia:latest`
- **Host:** daniel-server
- **Networks:** proxy · **Authelia:** N/A (it *is* Authelia)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Traefik points its `forward-auth` middleware here; toggling a service's
  `use_authelia` flag in `containers_list` is what puts it behind 2FA.
- `templates/configuration.yml.j2` — access control rules, OIDC clients, session/redis.
- `templates/users_database.yml.j2` — local users & argon2 password hashes.
- OIDC clients (e.g. Beszel) get their own secrets in `ansible/vars/secrets.yml`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "authelia"`
