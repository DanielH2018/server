# freshrss — RSS feed aggregator

FreshRSS with a small nginx feed-cache sidecar. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=freshrss` gates the rollout
- **Images:** `lscr.io/linuxserver/freshrss` (`freshrss_k8s_image`), `nginx`
  (`freshrss_k8s_cache_image`)
- **Route:** `freshrss.<domain>` · `freshrss.local.<domain>`, Authelia one_factor
- **Claim:** `freshrss-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **The nginx sidecar is the feed cache.**
- **Host: daniel-box (k8s), since 2026-08-05 — slice 2.** This role's ConfigMap embeds
  `files/nginx-feed-cache.conf`. Edit the cache conf / extensions HERE, then merge; `promote`
  applies it.
- **Port:** 80
- **Flux applies it.** Its `containers_list` entry is
  in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster when
  `promote` publishes and verifies the next bundle. Its image-bump PRs therefore draw no
  `land-wait` and no `probe.py health` run. authelia and traefik are flux-owned too, so the
  Kustomization lists them in `dependsOn`, and Flux applies this tag only after their
  Kustomizations are Ready. The FreshRSS Deployment is `Recreate`, so a change that rolls it takes
  the service down briefly. The feed cache is a one-replica `RollingUpdate`.
- **The `freshrss-snapshot` Job snapshots `freshrss-config` before each revision applies**.
  The freshrss Kustomization depends on `freshrss-snapshot`, so a revision applies
  only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks. A
  failed revision that changes freshrss writes the stateful hold, and `promote` does not reset
  `prod`, because a `:latest` digest swap may already have migrated the config volume. Nothing
  reverts the claim on its own: the operator follows the revert runbook in
  `docs/volume-snapshot-drills.md`. `k8s_autodeploy: true` still lets Renovate automerge the
  digest bump.
- **Its snapshot and rollout.** The `freshrss-snapshot` Job takes the pre-apply snapshot, and
  the Kustomization's `wait` holds the rollout. The `freshrss-feed-cache` entry in
  `manifests_extra_rollouts` makes the feed-cache Deployment a restart target, because nginx
  rereads `conf.d` only on start. The restart checksum the renderer stamps on it rolls nginx,
  so an edit to `files/nginx-feed-cache.conf` reaches it.

## Notable
- Bundles FreshRSS extensions shipped in `files/`: Karakeep button, Wallabag button,
  ToggleSidebar.
- The nginx sidecar (`files/nginx-feed-cache.conf`) proxies a **fixed set of upstreams**, one
  `location` each. It is not a general outbound-feed proxy, so adding a feed means editing the
  nginx conf.
  - `/` caches `rachelbythebay.com` for 24h, because that site blocks readers that poll often.
  - `/restofworld/` rewrites the unescaped `&dpr=` and `&crop=` in Rest of World's media URLs
    to `&amp;`. Without it FreshRSS rejects the feed as invalid XML. The rewrite stops working
    if the publisher changes those query parameters.
- **FreshRSS reaches the cache only through two settings in its own database.** Since FreshRSS
  1.30.0 an SSRF guard refuses private addresses, so the feed-cache is reachable only through
  these settings:
  - **Subscription URLs** use the FQDN `http://feed-cache.homelab.svc.cluster.local/...`. The
    guard resolves hosts with `dns_get_record`, which on this musl image ignores the pod's
    search domains, so the short name `feed-cache` fails as `Failed to resolve domain`.
  - **System configuration → internal host allowlist** holds that FQDN with the port, as
    `host:80`. Don't move it to the `INTERNAL_HOST_ALLOWLIST`
    env var. php-fpm runs with `clear_env` on, so the variable reaches the cron refresh but not
    a manual refresh. A non-empty env var also overrides the UI field.
- A site with no feed is subscribed through FreshRSS's own XPath scraping, not a new service.
  The `web-to-rss` skill finds the source, writes the rules and checks them with
  `scripts/freshrss/scrape_feed.py`. The rules live only in FreshRSS's config volume.

## Editing
- Extensions/cache: `files/` (the nginx conf is embedded into this role's ConfigMap)
