# freshrss — RSS feed aggregator

FreshRSS with a small nginx feed-cache sidecar. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `lscr.io/linuxserver/freshrss:latest` + `nginx:alpine` (feed cache)
- **Host:** daniel-server · **Port:** 80 · **URL:** `freshrss.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Bundles FreshRSS extensions shipped in `files/`: Karakeep button, Wallabag button,
  ToggleSidebar.
- The nginx sidecar (`files/nginx-feed-cache.conf`) caches outbound feed fetches.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Extensions/cache: `files/`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "freshrss"`
