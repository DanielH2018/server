# homepage — Application dashboard

The landing dashboard (gethomepage) with service tiles, widgets and bookmarks.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/gethomepage/homepage:latest`
- **Host:** daniel-server · **Port:** 3000 · **URL:** `homepage.<domain>` (Authelia: yes)
- **Networks:** apps, proxy, monitoring, media, homepage_private (spans many nets so
  widgets can reach the services they display)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Heavily templated: `services.yaml`, `widgets.yaml`, `settings.yaml`, `bookmarks.yaml`,
  `docker.yaml`, `custom.css` — edit these `.j2` files, not the live config.
- Reads container state via the read-only `docker-proxy`.
- Pulls calendar data from the internal `ical-proxy` over `homepage_private`.
- **Built-in healthcheck:** the `gethomepage/homepage` image ships its own Docker
  `HEALTHCHECK` (`wget --spider http://127.0.0.1:$PORT/api/healthcheck`, 10s interval),
  so Docker reports container health without a `healthcheck:` block in the compose
  template. `autoheal` and uptime-kuma's docker monitor rely on this native status —
  don't add a redundant compose `healthcheck`.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Dashboard cfg: `templates/*.yaml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "homepage"`
