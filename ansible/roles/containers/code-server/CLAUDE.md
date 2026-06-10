# code-server — Browser-based VS Code

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `my-code-server:latest` — **built locally** from `templates/Dockerfile.j2`
  (not a registry pull)
- **Host:** daniel-server · **Port:** 8443 · **URL:** `code-server.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Extensions are installed at build time via `files/extensions.sh` baked into the image.
- Because the image is built (`build: always` in the deploy task), bump it by redeploying
  this role — Watchtower won't update it.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Image: `templates/Dockerfile.j2`, `files/extensions.sh`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "code-server"`
