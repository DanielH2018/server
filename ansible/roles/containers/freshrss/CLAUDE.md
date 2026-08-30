# freshrss — RSS aggregator (remote node)

## At a glance
- **Images:** `lscr.io/linuxserver/freshrss` + `nginx:alpine` feed-cache sidecar (both digest-pinned; multi-arch indexes, arm64 verified 2026-08-30)
- **Host:** `daniel-cloud` (Oracle A1, arm64) — the **permanent primary**, not a replica
- **Route:** `freshrss.<domain>` · **Port:** 80 · **Networks:** `proxy`, `internal` · **Depends on:** traefik
- **Config:** `files/nginx-feed-cache.conf` → bind-mounted into the sidecar
- **State:** `containers/freshrss/config/` — the SQLite feed DB and unpacked extensions

## Notable
- **It moved here because it pulls feeds from the public internet.** Hosting it at home is
  what made it fragile; nothing about it needs home state or home hardware.
- **`./config` becomes the only copy of FreshRSS's state once the cluster role is retired.**
  Oracle Always Free gives 5 *manual* volume backups and no automated ones, so the nightly
  pull to the cluster must be armed before the cluster role goes away — not after.
- The `internal` network is **not** `internal: true` despite the name: the feed-cache nginx
  proxies `rachelbythebay.com` outbound, so isolating it would break the cache.
- The LSIO image needs the s6 privilege-drop caps plus `NET_BIND_SERVICE` for :80. If it
  fails to start on first deploy, read `docker logs freshrss` and add caps back rather than
  dropping the `cap_drop: ALL` baseline.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Feed-cache nginx: `files/nginx-feed-cache.conf` (bind-mounted, so `tasks/main.yml` passes
  `common_config_changed` — otherwise the sidecar keeps the old config after a deploy)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "freshrss" -e target=daniel-cloud`
