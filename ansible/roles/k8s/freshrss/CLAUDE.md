# freshrss — RSS feed aggregator

FreshRSS with a small nginx feed-cache sidecar. See repo-root `CLAUDE.md`.

## At a glance
- **Images:** `lscr.io/linuxserver/freshrss:latest` + `nginx:alpine` (feed cache)
- **Host: daniel-box (k8s), since 2026-08-05 — slice 2.** This role's ConfigMap embeds
  `files/nginx-feed-cache.conf`. Edit the cache conf / extensions HERE; deploy with
  `--tags freshrss` from daniel-box.
- **Port:** 80 · **URL:** `freshrss.<domain>` (Authelia: yes; forwards to the cluster via
  `bridge_hostname`)
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Notable
- Bundles FreshRSS extensions shipped in `files/`: Karakeep button, Wallabag button,
  ToggleSidebar.
- The nginx sidecar (`files/nginx-feed-cache.conf`) caches a **single hard-coded upstream**
  (`rachelbythebay.com`) — it's a targeted cache for that one feed, NOT a general
  outbound-feed proxy. Adding another cached feed means editing the nginx conf.

## Editing
- Extensions/cache: `files/` (the nginx conf is embedded into this role's ConfigMap)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "freshrss"`
