# homepage — Application dashboard

The landing dashboard (gethomepage) with service tiles, widgets and bookmarks. Live on
daniel-box since E3 (2026-08-12). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "homepage"`
- **Images:** `ghcr.io/gethomepage/homepage` (`homepage_k8s_image`), `alpine`
  (`homepage_k8s_init_image`)
- **Route:** `homepage.<domain>` · `homepage.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Depends on:** traefik, authelia

## Where the config lives
All of it renders into one Secret (`config-secret.yaml.j2`), which mounts read-only:

- `templates/config/{settings,bookmarks,widgets,services,docker,kubernetes}.yaml.j2` +
  `custom.css.j2` — every app config file, one level down because
  `validate/k8s_manifests.py` parses every `templates/*.j2` as a manifest and fails a
  `lookup('template')` that names one outside `config/`. `services.yaml` is the tile list;
  edit it here and nowhere else.
- `templates/icons-configmap.yaml.j2` — base64s the PNGs in `files/` into a ConfigMap.

Edit the `.j2` files, never the live config: homepage seeds any missing file into
`/app/config` at startup, which EROFSes on the read-only mount and crash-loops the pod.

## Notable

The grid and stat-block derivations, the browser measurements behind them and the per-widget
record are in `docs/homepage-widgets-and-layout.md`. Read that page before you change a
`columns:` count, a tile's stats or anything in `custom.css.j2`; the rules below are the ones
that bite from a config edit alone.

- **`mode: cluster` switches Ingress discovery ON unless `kubernetes.yaml.j2` says otherwise.**
  Upstream reads `traefik` and `gateway` as absent-is-off, but `ingress-list.js` destructures
  `const { ingress = true }`, so an omitted key is a cluster-wide `ingresses.networking.k8s.io`
  list on every page load. `rbac.yaml.j2` grants no such read, so the pod logged an RBAC denial
  per load for its whole life (#1428, #1459), burying the widget errors that matter.
  `ingress: false` removes the call rather than permitting it; this cluster routes with
  `IngressRoute` CRDs and owns no `Ingress` object, so the grant would list an empty set
  forever. The two halves are asserted together by
  `ansible/tests/k8s/test_k8s_manifests_rbac.py`.
- **A widget's credential goes in `key:`, never in `url:`.** Each widget's `proxy.js` logs the
  full request URL on any non-2xx, so a credential in a query string is published to the pod log
  and to Loki on the target's next outage. `jellyfin_api_key` leaked that way during the
  2026-09-09 Jellyfin crash loop (#1499) and was rotated on 2026-09-10. ENFORCED by
  `ansible/tests/services/test_homepage_widget_urls_carry_no_credentials.py`.
- **A widget dialling a ClusterIP needs `homepage_widget: true` on the target's entry.**
  The namespace baseline admits Traefik and Prometheus, so homepage's pod-to-pod call is
  denied by default and the tile fails as a widget-proxy error while homepage stays 1/1. The
  key adds homepage to the entry's fence, and `homepage_widget_url` builds the URL only for an
  entry carrying it (`ansible/filter_plugins/homepage_tiles.py`, #3691). ENFORCED on the
  rendered URLs by `ansible/tests/services/test_homepage_tiles_filter.py`. So a widget also
  deploys the target's fence. Headlamp's and pihole's widgets are the two exceptions, each
  behind a `DECIDED:` marker in `services.yaml.j2`.
- **A layout entry matches a group by NAME, and an unmatched one is silently dead.** `layout:`
  in `templates/config/settings.yaml.j2` and the group headings in `services.yaml.j2` are two
  lists that must agree. A layout key naming no group does nothing; a group with no layout key
  renders *below* every laid-out group at one column wide. Neither state errors, logs, or fails
  a render — check both files when a group appears in the wrong place or a column count has no
  effect.
- **`fields:` picks which blocks a widget renders, and an unknown name is dropped silently.**
  The valid names are upstream's, not the labels the tile displays, and three tiles take their
  blocks from somewhere other than `fields:` — the per-widget shapes are on the docs page.
- **A tile's `href` comes from the entry's `hostname`** through `homepage_href`. The tiles
  stay hand-placed, because their order is measured layout.
- **A block's HEADING cannot be changed in config — it is renamed in CSS.** `block.jsx` renders
  `t(label)`, an i18n lookup against a translation file baked into the image, and `fields:`
  selects which blocks render rather than what they are called. `custom.css.j2` collapses the
  original text and supplies the replacement as an `::after` `content`, for Uptime-Kuma,
  Speedtest, Karakeep and UPS. Tiles are matched by `href` so a reorder cannot mislabel one; the
  block index within a tile is positional and must agree with that widget's `fields:` order,
  ENFORCED by `ansible/tests/services/test_homepage_block_label_overrides.py`.
- **The Longhorn widget is configured across TWO files, and the wrong half is silent.**
  `providers.longhorn.url` in `templates/config/settings.yaml.j2` holds the connection;
  `templates/config/widgets.yaml.j2` holds only the display options. A `url:` written beside
  those options is ignored — the pod logs `<longhorn> Missing Longhorn URL` every refresh, the
  tile renders empty, and the Deployment stays 1/1. PR #1391 shipped it that way. ENFORCED by
  `ansible/tests/services/test_homepage_longhorn_widget_url.py`.
- **The tile list is CURATED, not a census of what is routed.** Nine link tiles were added on
  2026-09-09 and removed again the same day at the operator's request. They are routed and
  reachable; they are simply not wanted on the dashboard. Do not "restore" them by comparing
  `services.yaml.j2` against `containers_list`. `navidrome` and `livesync` are absent for a
  different reason — navidrome is scaled to 0 replicas and livesync has no IngressRoute at all.
- **The image ships a render of `/` made with no settings, and only a startup hook replaces
  it.** `next build` bakes the page, nothing expires it, and the pod serves it at 1/1 with
  `probe.py health homepage` exiting 0. The `lifecycle.postStart` hook in
  `templates/deployment.yaml.j2` calls `/api/revalidate` once at startup, which regenerates `/`
  from the config the pod can read. ENFORCED by the `homepage-revalidates-on-start` row of
  `ansible/tests/k8s/_workload_property_rows.py`; a browser tab reading
  `Homepage` is the symptom, and the measurements are on the docs page.
