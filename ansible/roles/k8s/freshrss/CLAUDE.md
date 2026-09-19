# freshrss — RSS feed aggregator

FreshRSS with a small nginx feed-cache sidecar. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "freshrss"`
- **Images:** `lscr.io/linuxserver/freshrss` (`freshrss_k8s_image`), `nginx`
  (`freshrss_k8s_cache_image`)
- **Route:** `freshrss.<domain>` · `freshrss.local.<domain>`, Authelia one_factor
- **Claim:** `freshrss-config`
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **The nginx sidecar is the feed cache.**
- **Host: daniel-box (k8s), since 2026-08-05 — slice 2.** This role's ConfigMap embeds
  `files/nginx-feed-cache.conf`. Edit the cache conf / extensions HERE; deploy with
  `--tags freshrss` from daniel-box.
- **Port:** 80

## Notable
- Bundles FreshRSS extensions shipped in `files/`: Karakeep button, Wallabag button,
  ToggleSidebar.
- The nginx sidecar (`files/nginx-feed-cache.conf`) caches a **single hard-coded upstream**
  (`rachelbythebay.com`) — it's a targeted cache for that one feed, NOT a general
  outbound-feed proxy. Adding another cached feed means editing the nginx conf.

## Editing
- Extensions/cache: `files/` (the nginx conf is embedded into this role's ConfigMap)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "freshrss"`
