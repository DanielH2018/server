# homepage widgets and layout — the CSS derivations and the per-widget record

Working-out moved off `ansible/roles/k8s/homepage/CLAUDE.md` (#2995), which a session reads on
every touch of the dashboard. The role doc keeps the editing rules; this page keeps the grid and
stat-block derivations, the browser measurements behind them, and the per-widget record of what
is on the dashboard and why.

## The Top Row grid

The two `Top Row` columns are separate grids, so nothing aligns them on its own, and a `calc()`
pin on the calendar broke every time a tile title wrapped at a narrower width. The wrapper
gethomepage puts between a group and its list carries Tailwind's `block!`, so it can never be a
flex container and the list can never be a flex item — that is why ten attempts at a flex or
grid chain all failed.

`custom.css.j2` instead makes the group a flex column, lets the wrapper grow as a block, then
gives the list `height: calc(100% - 0.75rem)` and splits it into eight equal `minmax(0, 1fr)`
tracks with the same `0.75rem` gap the Services grid uses. The calendar spans six tracks and
each link row takes one: with track `t = (r - g) / 2`, six tracks and five gaps are three
Services rows (`3r + 2g`), and two tracks plus a gap are one (`2t + g = r`). Nothing is a pixel
constant, so it holds for wrapped titles and every width. A Services group that renders a
different row count needs twice that many tracks and a matching `span`.

`Top Row` is ITSELF a `div.services-group` wrapping the two column groups, so a
`:has(#my-calendar)` selector matches the outer group too and reaches the Services list — which
shipped Services eight 112px tracks and a 980px column on 2026-09-10. The selectors are scoped
with `:has(> div > ul > #my-calendar)` and `:has(> #my-calendar)` for that reason, and
`#my-calendar` carries `contain: size` so its natural height cannot outgrow the Services column
and set the row height.

### The calendar list item is sized by its grid tracks, never left at `auto`

The card carries `height: 100%`, which resolves only against a definite `<li>` height. A
stretched grid item is definite; an `align-self: start` one computes to `auto`, so the card took
its content height and a `max-height` on the `<li>` clamped only the `<li>`. An `<li>` does not
clip, so the overflow painted over the first row of links; measured at 1280px, a 360px `<li>`
holding a 460px card. `minmax(0, 1fr)` tracks keep a long event list from growing the `<li>` the
other way. `document.elementFromPoint` cannot see this — the links' stretched anchors win the
hit test — so compare `card.getBoundingClientRect().bottom` against the `<li>`'s.

### A bookmark group cannot be laid out inside `Top Row`, at any depth

Nested under `Calendar:` and as a sibling of Services and Calendar were both tried and both
silently ignored: gethomepage v1.13.2 drops the group out of `Top Row` and renders it LAST at
full width, exactly as an unlaid-out group does (#1488, #1490). Nothing logs it and every
repo-side check stays green. The personal links therefore live in the `Calendar` group of
`services.yaml.j2` as SERVICE tiles, `bookmarks.yaml.j2` is an empty list, and there is no
`Bookmarks:` layout entry. That group is five columns wide so the links fill one row, with
`#my-calendar { grid-column: 1 / -1 }` spanning the calendar back across all five.

## A group's `columns:` is derived from its tile count

A count the columns do not divide leaves an orphan on the last row and a hole beside it, and in
`Top Row` it also makes the Services column overshoot the Calendar beside it — nine tiles at two
columns ran 600px against a 372px Calendar, so 228px sat dead under the Calendar and 424x120
beside Home Assistant. Three columns divides nine exactly and lands at 396px. When you add or
remove a tile in a laid-out group, re-check that the count still divides, and measure in the
browser rather than reasoning about it: `getBoundingClientRect` on the `ul.services-list` gives
the column height.

The groups split by WIDGET, not by topic. A widget-carrying tile is several lines tall and a
link-only tile is one line, so a group holding both leaves ragged holes. `Services` holds every
widget-carrying tile bar the calendar; `Admin & Tools` holds the link-only ones, four per row.
Two groups became four on 2026-09-10: the `Media` group's three surviving tiles moved into
`Services` to make that column four rows deep, and `Admin` and `Tools` merged into one group of
eight, which still divides by four. Jellyfin was dropped rather than moved, its widget having
erred since #1457.

### An icon-only tile stretches its name anchor, rather than hiding it

gethomepage renders a tile's name inside its own `<a>` carrying the same `href` as the icon
anchor. `display: none` on that anchor leaves only the 48x32 icon as a hit target inside a 165px
tile — most of the tile looks like a button and does nothing. `custom.css.j2` instead covers the
card with that anchor (`position: absolute; inset: 0`, the card is already `relative`) and hides
only the name TEXT. Verify with `document.elementFromPoint` at a tile's centre and its four
edges, not by eye.

## Stat blocks

**A clipped widget is invisible to a check on the tile's `<li>`.** gethomepage lays a widget's
stat blocks out as a non-wrapping flex row inside a card carrying Tailwind's `overflow-clip`, so
a row wider than the card is cut off on the right with no console error, no pod log and a
still-rendering tile. The clipping happens on the `div.service-card` INSIDE the `<li>`, so the
`<li>`'s own `scrollWidth` equals its `clientWidth` and reads clean — that is exactly how the
three-column change shipped having cut 63px off the Karakeep tile's fourth stat. Test every
descendant: `li.querySelectorAll('*')` filtered on `scrollWidth > clientWidth + 1 &&
clientWidth > 0`. `custom.css.j2` wraps those blocks two per row, which also equalises tile
height — every widget carries two to four stats, so all of them occupy two stat rows.

**Stat blocks are laid out by COUNT.** One spans the full width, two or four wrap two to a row,
and three span the full width. Two and four divide evenly at a 50% basis; one and three do not,
so `custom.css.j2` gives those their own basis via `:has(> :first-child:last-child)` and
`:has(> :nth-child(3):last-child)` — an only child, and a third child that is also the last. A widget whose stat count changes moves between these automatically; nothing needs
updating by hand. Since the 2026-09-10 pruning only the one- and two-stat cases occur.

**Reserve a stat row or a whole grid row shrinks.** `.services-list` sizes each grid row to its
tallest member, so a row whose tiles all hold fewer stat rows than the rows below it drops on its
own — uniform across, visibly different down. `min-height` on `.service-container` makes the
tallest case the only case. It was two rows (`6.5rem`) while widgets carried up to four stats;
the 2026-09-10 pruning capped every widget at two, which fit one row, so it is one row
(`3.25rem`). Raise it again if a widget grows past two stats.

**Wrapped stat blocks must not grow.** `custom.css.j2` sets `flex: 0 0 calc(50% - 0.5rem)`, and
the leading `0` is load-bearing. With `flex-grow: 1` an odd stat count stretches the last block
across the full row, so a three-stat widget renders 128px, 128px, 264px against a uniform 128px
on a four-stat one — same tile height, visibly different blocks. Growth off, an odd count leaves
its last half-row empty and every block matches.

### Which stats a widget can render

The valid `fields:` names for a widget type are the `label="<type>.<name>"` list in
`src/widgets/<type>/component.jsx` upstream — not the labels the tile displays, which are
translations. Every widget here was pruned to at most two stats on 2026-09-10 at the operator's
request, and two of the requested stats do not exist: the Karakeep widget has no *unarchived*
count (`bookmarks` and `archived` are the two numbers it subtracts from, and no widget type here
can express arithmetic), and the `peanut` widget names its two `battery_charge` and `ups_status`.

Two tiles take a different shape. The Home Assistant widget has no `fields:` at all — its
`custom:` entries ARE the blocks, so pruning it means deleting entries. The Headlamp tile's
blocks come from `mappings:` paired positionally to the PromQL operands in `defaults/main.yml`,
so pruning it means editing both files together.

The `::after` rename in `custom.css.j2` uses `font-size: 0` rather than `visibility: hidden`,
because the box must be sized by the NEW text: "Battery Charge" wrapped to two lines in an 85px
block and took its whole grid row from 120px to 136px.

## The per-widget record

- **The Headlamp tile's widget reads Prometheus, not Headlamp.** Headlamp exposes no service API
  — its backend only proxies the Kubernetes API — and dialling its ClusterIP is fenced off on
  purpose (`roles/k8s/headlamp/templates/networkpolicy.yaml.j2`, re-asserted every deploy by that
  role's policy-probe Job). So the tile shows cluster-state counts from a `customapi` widget
  against `prometheus.<observability-ns>:9090`, with the PromQL in `defaults/main.yml` as
  `homepage_k8s_headlamp_cluster_query`. That call is cross-namespace, so `prometheus-callers`
  (`roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2`) names `homepage` — a
  widget-proxy error on that tile is the symptom of it being dropped.
- **The Longhorn widget dials `longhorn-frontend`, not `longhorn-backend`.** Longhorn's own
  chart-owned `longhorn-manager` policy admits six same-namespace components and nothing else,
  and editing it means editing an object the Longhorn deploy would revert. `longhorn-frontend` is
  selected by no policy at all and proxies `/v1` through as `app: longhorn-ui`, which that policy
  does admit. The node-local-manager trap does not apply here: the caller is a pod and the target
  is the frontend Service.
- **Three widgets are deliberately absent**, each blocked on a credential rather than on
  plumbing: Grafana (#1384 — needs a service-account token; only the admin password exists),
  CrowdSec (#1385 — a LAPI machine credential is read/write on decisions) and Healthchecks
  (#1386 — needs a project API key created in the UI). Read the issue before adding one; each
  records why it was not simply plumbed in. Traefik was a fourth (#1383 — `api.insecure: false`
  and the `DECIDED:` marker in `roles/k8s/traefik/templates/dashboard-ingressroute.yaml.j2` mean
  `/api` is served nowhere the widget can reach); its tile was replaced by the deploy queue on
  2026-09-10, so it has no tile to add a widget to.
- **A re-added Jellyfin tile needs `version: 2`.** The tile was dropped on 2026-09-10 and stays
  dropped; this is what to do if it comes back. The widget's default v1 mappings carry Emby's
  path prefix, which this Jellyfin 404s, and which is what erred on every refresh in #1457.
  `version: 2` in the widget block selects the `SessionsV2`/`CountV2` mappings instead:
  `Sessions` and `Items/Counts`, with the key sent as an `Authorization` header and nothing
  credential-shaped in the URL. Restoring the tile is more than a `services.yaml.j2` edit — the
  `Services` group is pinned at twelve tiles over three columns, and the calendar column's
  `grid-template-rows: repeat(8, …)` in `custom.css.j2` encodes that FOUR-row count (two tracks
  per Services row), so a thirteenth tile means re-deriving both and re-measuring in the browser.
- The `docker.yaml` status dots have no k8s equivalent, so `docker.yaml.j2` renders empty and
  `services.yaml.j2` drops the matching `server:`/`container:` keys — tiles render without a dot
  rather than erroring on a `my-docker` host that does not exist here.
- The calendar's own data comes from the internal `ical-proxy`.

## A tab reading `Homepage` means the page rendered with no settings

The browser tab title comes from `title:` in `templates/config/settings.yaml.j2`, and `Homepage`
is the app's default when it has none. The `<title>` fallback in gethomepage's
`src/pages/index.jsx` at v1.13.2 renders `initialSettings.title || "Homepage"`, and
`getStaticProps` returns `initialSettings: {}` when it renders with no settings — so a tab
reading `Homepage` says the page rendered with NO settings, not that the setting was dropped.
The `-m ui` smoke test pins the configured title for exactly this reason; issue #1399 misread a
`Homepage` failure as a stale expectation.

That is the symptom of the baked page the image ships (#1414, settled 2026-09-06 against the
pinned digest). `getStaticProps` carries no `revalidate` key, so `next build` bakes `/` into the
image against the build's own skeleton config, whose `settings.yaml` is empty. In the image:
`/app/.next/server/pages/en.json` reads `"initialSettings":{}`, `en.html` reads `<title
data-next-head="">Homepage</title>`, and `prerender-manifest.json` records
`"initialRevalidateSeconds": false` — nothing expires it. Upstream re-renders only when a
browser's stored `/api/hash` value MISMATCHES the pod's (`src/pages/index.jsx`), and a browser
with no stored value stores it and triggers nothing. A fresh pod visited only by fresh browsers
therefore served the page without settings for the container's whole life, at 1/1 and with
`probe.py health homepage` exiting 0. The `lifecycle.postStart` hook in
`templates/deployment.yaml.j2` calls `/api/revalidate` once at startup, which regenerates `/`
from the config the pod can read. The catch branch of `getStaticProps` is a DIFFERENT failure
and logs `<index>`; the baked page came from the success path and logs nothing, so a pod-log gate
cannot see this one.
