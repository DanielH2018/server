# homepage — Application dashboard

The landing dashboard (gethomepage) with service tiles, widgets and bookmarks. Live on
daniel-box since E3 (2026-08-12). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/gethomepage/homepage:latest`
- **URL:** `homepage.<domain>` · **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "homepage"`

## Where the config lives
All of it renders into one Secret (`config-secret.yaml.j2`), which mounts read-only:

- `templates/config/{settings,bookmarks,widgets}.yaml.j2` + `custom.css.j2` — moved here
  from the retired Docker role, which used to own them. They sit one level down because
  `validate/k8s_manifests.py` parses every `templates/*.j2` as a manifest, and `custom.css`
  is not YAML.
- `templates/services.yaml.j2`, `docker.yaml.j2`, `kubernetes.yaml.j2` — always this role's
  own. `services.yaml` is the tile list; edit it here and nowhere else.
- `templates/icons-configmap.yaml.j2` — base64s the PNGs in `files/` into a ConfigMap.

Edit the `.j2` files, never the live config: homepage seeds any missing file into
`/app/config` at startup, which EROFSes on the read-only mount and crash-loops the pod.

## Notable
- Pulls calendar data from the internal `ical-proxy`.
- **`mode: cluster` switches Ingress discovery ON unless `kubernetes.yaml.j2` says otherwise.**
  Upstream reads `traefik` and `gateway` as absent-is-off, but `ingress-list.js` destructures
  `const { ingress = true }`, so an omitted key is a cluster-wide `ingresses.networking.k8s.io`
  list on every page load. `rbac.yaml.j2` grants no such read, so the pod logged an RBAC denial
  per load for its whole life (#1428, #1459) — no user-visible effect, but a 13-line log where
  two lines were the denial buries the widget errors that matter. `ingress: false` removes the
  call rather than permitting it; this cluster routes with `IngressRoute` CRDs and owns no
  `Ingress` object, so the grant would list an empty set forever. The two halves are asserted
  together by `ansible/tests/k8s/test_k8s_manifests_rbac.py`.
- **A widget's credential goes in `key:`, never in `url:`.** Each widget's `proxy.js` logs the
  full request URL on any non-2xx, so a credential in a query string is published to the pod log
  and to Loki on the target's next outage. `jellyfin_api_key` leaked that way during the
  2026-09-09 Jellyfin crash loop (#1499) and was rotated on 2026-09-10. ENFORCED by
  `ansible/tests/services/test_homepage_widget_urls_carry_no_credentials.py`.
- **A re-added Jellyfin tile needs `version: 2`.** The tile was dropped on 2026-09-10 (see the
  group note below) and stays dropped; this is what to do if it comes back. The widget's default
  v1 mappings are `emby/Sessions?api_key={key}` and `emby/Items/Counts?api_key={key}` — Emby's
  path prefix, which this Jellyfin 404s, and which is what erred on every refresh in #1457.
  `version: 2` in the widget block selects the `SessionsV2`/`CountV2` mappings instead: `Sessions`
  and `Items/Counts`, with the key sent as `Authorization: MediaBrowser Token=...` and nothing
  credential-shaped in the URL. Restoring the tile is more than a `services.yaml.j2` edit — the
  `Services` group is pinned at twelve tiles over three columns, and the calendar column's
  `grid-template-rows: repeat(8, …)` in `custom.css.j2` encodes that FOUR-row count (two tracks
  per Services row), so a thirteenth tile means re-deriving both and re-measuring in the browser.
- **A layout entry matches a group by NAME, and an unmatched one is silently dead.** `layout:`
  in `templates/config/settings.yaml.j2` and the group headings in `services.yaml.j2` are two
  lists that must agree. A layout key naming no group does nothing (a `Monitoring:` entry sat
  there until 2026-09-09 for a group this dashboard has never declared); a group with no layout
  key renders *below* every laid-out group at one column wide (`Tracking` sat that way). Neither
  state errors, logs, or fails a render — check both files when a group appears in the wrong
  place or a column count has no effect.
- **A group's `columns:` is derived from its tile count, not chosen.** A count the columns do
  not divide leaves an orphan on the last row and a hole beside it, and in `Top Row` it also
  makes the Services column overshoot the Calendar beside it — nine tiles at two columns ran
  600px against a 372px Calendar, so 228px sat dead under the Calendar and 424x120 beside Home
  Assistant. Three columns divides nine exactly and lands at 396px. When you add or remove a
  tile in a laid-out group, re-check that the count still divides, and measure in the browser
  rather than reasoning about it — `getBoundingClientRect` on the `ul.services-list` gives the
  column height.
- **A clipped widget is invisible to a check on the tile's `<li>`.** homepage lays a widget's
  stat blocks out as a non-wrapping flex row inside a card carrying Tailwind's `overflow-clip`,
  so a row wider than the card is cut off on the right with no console error, no pod log and a
  still-rendering tile. The clipping happens on the `div.service-card` INSIDE the `<li>`, so the
  `<li>`'s own `scrollWidth` equals its `clientWidth` and reads clean — that is exactly how the
  three-column change shipped having cut 63px off Karakeep's fourth stat. Test every descendant:
  `li.querySelectorAll('*')` filtered on `scrollWidth > clientWidth + 1 && clientWidth > 0`.
  `custom.css.j2` now wraps those blocks two per row, which also equalises tile height — every
  widget carries two to four stats, so all of them occupy two stat rows.
- **The groups split by WIDGET, not by topic.** A widgeted tile is several lines tall and a
  link-only tile is one line, so a group holding both leaves ragged holes. `Services` holds every
  widgeted tile bar the calendar and Crypto; `Admin & Tools` holds the link-only ones, four per
  row. Two groups became four on 2026-09-10: the `Media` group's three surviving tiles moved into
  `Services` to make that column four rows deep, and `Admin` and `Tools` merged into one group of
  eight, which still divides by four. Jellyfin was dropped rather than moved (its widget had been
  erroring since #1457).
- **The calendar column auto-sizes to the Services column, and the wrapper between them is
  `display: block !important`.** The two Top Row columns are separate grids, so nothing aligns
  them on its own, and a `calc()` pin on the calendar broke every time a tile title wrapped at a
  narrower width. The wrapper homepage puts between a group and its list carries Tailwind's
  `block!`, so it can never be a flex container and the list can never be a flex item — that is
  why ten attempts at a flex or grid chain all failed. `custom.css.j2` instead makes the group a
  flex column, lets the wrapper grow as a block, then gives the list `height: calc(100% - 0.75rem)`
  and splits it into eight equal `minmax(0, 1fr)` tracks with the same 0.75rem gap the Services
  grid uses. The calendar spans six tracks and each link row takes one: with track t = (r - g) / 2,
  six tracks and five gaps are three Services rows (3r + 2g) and two tracks plus a gap are one
  (2t + g = r). Nothing is a pixel constant, so it holds for wrapped titles and every width. A
  Services group that renders a different row count needs 2x that many tracks and a matching
  `span`. `Top Row` is ITSELF a `div.services-group` wrapping the two column groups, so a
  `:has(#my-calendar)` selector matches the outer group too and reaches the Services list —
  which shipped Services eight 112px tracks and a 980px column on 2026-09-10. The selectors are
  scoped with `:has(> div > ul > #my-calendar)` and `:has(> #my-calendar)` for that reason, and
  `#my-calendar` carries `contain: size` so its natural height cannot outgrow the Services
  column and set the row height.
- **The calendar `<li>` must be sized by its grid tracks, never left at `auto`.** The card
  carries `height: 100%`, which resolves only against a definite `<li>` height. A stretched grid
  item is definite; an `align-self: start` one computes to `auto`, so the card took its content
  height and a `max-height` on the `<li>` clamped only the `<li>`. An `<li>` does not clip, so the
  overflow painted over the first row of links; measured at 1280px, a 360px `<li>` holding a 460px
  card. `minmax(0, 1fr)` tracks keep a long event list from growing the `<li>` the other way.
  `document.elementFromPoint` cannot see this — the links' stretched anchors win the hit test —
  so compare `card.getBoundingClientRect().bottom` against the `<li>`'s.
- **An icon-only tile must stretch its name anchor, not hide it.** Homepage renders a tile's
  name inside its own `<a>` carrying the same href as the icon anchor. `display: none` on that
  anchor leaves only the 48x32 icon as a hit target inside a 165px tile — most of the tile looks
  like a button and does nothing. `custom.css.j2` instead covers the card with that anchor
  (`position: absolute; inset: 0`, the card is already `relative`) and hides only the name TEXT.
  Verify with `document.elementFromPoint` at a tile's centre and its four edges, not by eye.
- **`fields:` picks which blocks a widget renders, and an unknown name is dropped silently.**
  Every widget on this dashboard was pruned to at most two stats on 2026-09-10 at the operator's
  request. The valid names for a type are the `label="<type>.<name>"` list in
  `src/widgets/<type>/component.jsx` upstream — not the labels the tile displays, which are
  translations. Two of the requested stats do not exist: karakeep has no *unarchived* count
  (`bookmarks` and `archived` are the two numbers it subtracts from, and no widget type here can
  express arithmetic), and peanut names its two `battery_charge` and `ups_status`. The Home
  Assistant widget has no `fields:` at all — its `custom:` entries ARE the blocks, so pruning it
  means deleting entries. The Headlamp tile is a third shape again: its blocks come from
  `mappings:` paired positionally to the PromQL operands in `defaults/main.yml`, so pruning it
  means editing both files together.
- **A block's HEADING cannot be changed in config — it is renamed in CSS.** `block.jsx` renders
  `t(label)`, an i18n lookup against `public/locales/<lang>/common.json` baked into the image, and
  `fields:` selects which blocks render rather than what they are called. `custom.css.j2` collapses
  the original text with `font-size: 0` and supplies the replacement as a `::after` `content`, for
  Uptime-Kuma, Speedtest, Karakeep and UPS. `font-size: 0` and not `visibility: hidden`, because
  the box must be sized by the NEW text: "Battery Charge" wrapped to two lines in an 85px block
  and took its whole grid row from 120px to 136px. Tiles are matched by href so a reorder cannot
  mislabel one; the block index within a tile is positional and must agree with that widget's
  `fields:` order, ENFORCED by `ansible/tests/services/test_homepage_block_label_overrides.py`.
- **Stat blocks are laid out by COUNT: one spans the full width, two or four wrap two to a row,
  and three span the full width.** Two and four divide evenly at a 50% basis; one and three do
  not, so `custom.css.j2` gives those their own basis via `:has(> :first-child:last-child)` and
  `:has(> :nth-child(3):last-child)` — "an only child", and "a third child that is also the
  last". A widget whose stat count changes moves between these automatically; nothing needs
  updating by hand. Since the 2026-09-10 pruning only the one- and two-stat cases occur.
- **Reserve a stat row or a whole grid row shrinks.** `.services-list` sizes each grid row to
  its tallest member, so a row whose tiles all hold fewer stat rows than the rows below it
  drops on its own — uniform across, visibly different down. `min-height` on
  `.service-container` makes the tallest case the only case. It was two rows (6.5rem) while
  widgets carried up to four stats; the 2026-09-10 pruning capped every widget at two, which fit
  one row, so it is one row (3.25rem) now. Raise it again if a widget grows past two stats.
- **Wrapped stat blocks must not grow.** `custom.css.j2` sets `flex: 0 0 calc(50% - 0.5rem)`,
  and the leading `0` is load-bearing. With `flex-grow: 1` an odd stat count stretches the last
  block across the full row, so a three-stat widget renders 128px, 128px, 264px against a
  uniform 128px on a four-stat one — same tile height, visibly different blocks. Growth off, an
  odd count leaves its last half-row empty and every block matches.
- **A link-only tile is a config-only change; a widget is not.** A `widget:` dialling a ClusterIP
  needs the target's NetworkPolicy to name `app: homepage` (see below), so adding a service to
  the dashboard and giving it a live widget are two separate pieces of work. The `Admin` and
  `Tools` groups exist to make the first one cheap — before they landed, sixteen routed web UIs
  were reachable only by remembering the hostname.
- **The tile list is CURATED, not a census of what is routed.** Nine link tiles were added on
  2026-09-09 and removed again the same day at the operator's request — prowlarr, bazarr, tdarr,
  authelia, healthchecks, wg-easy, zigbee2mqtt, littlelink and bento-pdf. They are routed and
  reachable; they are simply not wanted on the dashboard. Do not "restore" them by comparing
  `services.yaml.j2` against `containers_list`. `navidrome` and `livesync` are absent for a
  different reason — navidrome is scaled to 0 replicas and livesync has no IngressRoute at all.
- **A bookmark group cannot be laid out inside `Top Row`, at any depth.** Nested under
  `Calendar:` and as a sibling of Services and Calendar were both tried and both silently
  ignored: homepage v1.13.2 drops the group out of Top Row and renders it LAST at full width,
  exactly as an unlaid-out group does (#1488, #1490). Nothing logs it and every repo-side check
  stays green. The personal links therefore live in the `Calendar` group of `services.yaml.j2`
  as SERVICE tiles, `bookmarks.yaml.j2` is an empty list, and there is no `Bookmarks:` layout
  entry. That group is five columns wide so the links fill one row, with
  `#my-calendar { grid-column: 1 / -1 }` spanning the calendar back across all five.
- **The Headlamp tile's widget reads Prometheus, not Headlamp.** Headlamp exposes no service
  API — its backend only proxies the Kubernetes API — and dialling its ClusterIP is fenced off
  on purpose (`roles/k8s/headlamp/templates/networkpolicy.yaml.j2`, re-asserted every deploy by
  that role's netpol-probe Job). So the tile shows cluster-state counts from a `customapi`
  widget against `prometheus.<observability-ns>:9090`, with the PromQL in `defaults/main.yml`
  as `homepage_k8s_headlamp_cluster_query`. That call is cross-namespace, so
  `prometheus-callers` (`roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2`)
  names `homepage` — a widget-proxy error on that tile is the symptom of it being dropped.
- **A widget dialling a ClusterIP needs the target's NetworkPolicy to name `app: homepage`.**
  The namespace baseline admits traefik and prometheus, so pod-to-pod from homepage is denied by
  default and the tile fails as a widget-proxy error while homepage stays 1/1. ENFORCED by
  `ansible/tests/services/test_homepage_widget_netpol_edges.py`, which resolves each widget URL's
  Service to the pod label it selects and checks the rendered policies.
- **The longhorn widget is configured across TWO files, and the wrong half is silent.**
  `providers.longhorn.url` in `templates/config/settings.yaml.j2` holds the connection;
  `templates/config/widgets.yaml.j2` holds only the display options. A `url:` written beside those
  options is ignored — the pod logs `<longhorn> Missing Longhorn URL` every refresh, the tile
  renders empty, and the Deployment stays 1/1. PR #1391 shipped it that way. ENFORCED by
  `ansible/tests/services/test_homepage_longhorn_widget_url.py`.
- **The longhorn widget dials `longhorn-frontend`, not `longhorn-backend`.** Longhorn's own
  chart-owned `longhorn-manager` policy admits six same-namespace components and nothing else, and
  editing it means editing an object the Longhorn deploy would revert. `longhorn-frontend` is
  selected by no policy at all and proxies `/v1` through as `app: longhorn-ui`, which that policy
  does admit. The node-local-manager trap does not apply here: the caller is a pod and the target
  is the frontend Service.
- **Three widgets are deliberately absent**, each blocked on a credential rather than on
  plumbing: grafana (#1384 — needs a service-account token; only the admin password exists),
  crowdsec (#1385 — a LAPI machine credential is read/write on decisions) and healthchecks
  (#1386 — needs a project API key created in the UI). Read the issue before adding one; each
  records why it was not simply plumbed in. Traefik was a fourth (#1383 — `api.insecure: false`
  and the `DECIDED:` marker in `roles/k8s/traefik/templates/dashboard-ingressroute.yaml.j2` mean
  `/api` is served nowhere the widget can reach); its tile was replaced by the deploy queue on
  2026-09-10, so it has no tile to add a widget to.
- The `docker.yaml` status dots have no k8s equivalent yet, so `docker.yaml.j2` renders empty
  and `services.yaml.j2` drops the matching `server:`/`container:` keys — tiles render
  dot-less rather than erroring on a `my-docker` host that does not exist here.
- **The browser tab title comes from `title:` in `templates/config/settings.yaml.j2`, and
  `Homepage` is the app's config-less default.** `src/pages/index.jsx:410` in gethomepage
  v1.13.2 renders `initialSettings.title || "Homepage"`, and `getStaticProps` returns
  `initialSettings: {}` when it renders with no settings — so a tab reading `Homepage` says
  the page rendered with NO settings, not that the setting was dropped. The `-m ui` smoke test
  pins the configured title for exactly this reason; issue #1399 misread a `Homepage` failure
  as a stale expectation.
- **The image ships a config-less render of `/`, and only a startup hook replaces it** (#1414,
  settled 2026-09-06 against the pinned digest). `getStaticProps` carries no `revalidate` key,
  so `next build` bakes `/` into the image against the build's own skeleton config, whose
  `settings.yaml` is empty. In the image: `/app/.next/server/pages/en.json` reads
  `"initialSettings":{}`, `en.html` reads `<title data-next-head="">Homepage</title>`, and
  `prerender-manifest.json` records `"initialRevalidateSeconds": false` — nothing expires it.
  Upstream re-renders only when a browser's stored `/api/hash` value MISMATCHES the pod's
  (`src/pages/index.jsx`), and a browser with no stored value stores it and triggers nothing.
  A fresh pod visited only by fresh browsers therefore served the config-less page for the
  container's whole life, at 1/1 and with `probe.py health homepage` exiting 0. The
  `lifecycle.postStart` hook in `templates/deployment.yaml.j2` calls `/api/revalidate` once at
  startup, which regenerates `/` from the config the pod can read. ENFORCED by
  `ansible/tests/services/test_homepage_revalidates_on_start.py`. The catch branch of
  `getStaticProps` is a DIFFERENT failure and logs `<index>`; the baked page came from the
  success path and logs nothing, so a pod-log gate cannot see this one.
