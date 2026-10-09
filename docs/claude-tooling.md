# Claude tooling in this repo — reference

The long form of `CLAUDE.md` → *Claude Tooling in This Repo*. That section keeps one directive
per tool, the part that has to be in context whether or not anyone opens this page. Everything
here is detail you read when you are working on one of these tools, or when one of them has
just surprised you. A hook's own docstring is the fullest record of its rules and incidents,
and a hook that denies prints its reason.

## `scripts/diagnostics/probe.py`

Read-only homelab diagnostics, allow-listed (no prompt). It resolves the live container IP via
`docker inspect`, so prefer it over curling bridge IPs, which change on recreate.

To see every subcommand with a one-line description, run
`uv run python scripts/diagnostics/probe.py --list`. The list comes from `SUBCOMMANDS`/`REGISTRY`
in `probe_lib/subcommands.py`, built on the shared `scripts/lib/cli_registry.py` (the same
named-entry shape monitor-bridge's check registry uses, in that role's `files/registry.py`).

The registry also drives dispatch. `probe.py`'s `main()` builds its `handlers` table from the
entries flagged `"handler"`. Four subcommands with a callable (`health`, `targets`, `metric`,
`loki-query`) answer from it only for some flags, so `main()` routes them by hand
(`subcommands.ROUTED_IN_MAIN`). A subcommand with no callable streams through `plan()` in
`probe_lib/curl_pipeline.py`. The argument parsing stays in `probe_lib/cli_parser.py`, because each
subcommand's arguments differ too much for a registry row to describe.
`scripts/diagnostics/tests/test_probe_registry.py` checks that the subcommands `cli_parser.py`
accepts equal the registry's names, and that every `probe_lib` module with a `run_*`/`main` entry point is
registered.

### Measurements and invariant checks: `b2-spend`, `vip-placement`, `readonly-rbac`, `shed-set`

Four subcommands turn a fact an operator once had to remember into one they can re-derive. Each
has its own test under `scripts/diagnostics/tests/`.

- **`b2-spend [--since 24h]`** sums the Class B spend per volume out of Longhorn's own "changed
  blocks" log lines, because B2 has no usage API (`probe_lib/b2_ledger.py`). It reads Loki
  only and spends nothing on B2. The model covers backups only, so it is a lower bound on the
  console's figure. It joins each volume to its `spec.backupTargetName` and charges only the
  B2-target volumes to the "Class B measured" figure; the R2-target ones print as their own
  excluded subtotal, because R2's caps are monthly and vast. A volume whose target does not
  resolve counts against B2 — a failed join must not read as a quiet day during a cap incident.
- **`vip-placement`** reads the Services, EndpointSlices, L2Advertisements and Nodes, and exits
  1 naming any `externalTrafficPolicy: Local` MetalLB VIP with no Ready endpoint on the node
  that announces it (`probe_lib/vip_placement.py`). Host probes stay green through that
  blackout, which is why it exists.
- **`readonly-rbac`** asks live RBAC whether the SA plain `kubectl` runs as still refuses
  `get`/`list` on Secrets and `create`/`delete` on pods, and exits 1 naming any verb it gained
  (`probe_lib/readonly_rbac.py`).
- **`shed-set`** prints the Deployments and StatefulSets to scale to zero when one node is
  lost, so the tiered services fit on the survivor (`probe_lib/shed_set.py`). The set is every
  k8s entry with no `tier:`, derived by `shed_entries` in `filter_plugins/service_tier.py`, and
  each role is rendered to name its scalable workloads. It reads the inventory only.

### `alerts [--days N --check X]`

Reconstructs DOWN alert history from Loki, because Kuma keeps only current state — one row per
firing episode. The same view is the "Alert History" Grafana board (Infrastructure folder).

It reads **two** streams: monitor-bridge's container log, and the `{job="syslog"}` `status=down`
lines the host crons emit, which push Kuma directly and so have no other durable record. Until
2026-08-22 it read only the first, and the whole backup/drift plane left no episode anywhere.

**Every episode row carries both ends, in UTC.** Two things made this view misdate an incident,
and both are fixed: rows were stamped `America/Chicago` with no marker, five hours off the
`journalctl --utc` output an operator compares them against, and only the episode's start was
printed. **Each check's splitting gap is now derived from its own sample cadence**, because a
fixed 30 minutes matched the `*/30` health crons exactly — a second of cron jitter started a new
episode, so one 13.5-hour outage on 2026-09-04 rendered as 16 rows, none of them carrying the
onset. `--gap-min` still pins the gap by hand for every check. See #1104.

**Three things made the episode view under-report, and all three are fixed (#1782).**
monitor-bridge stopped printing its own `[timestamp]` prefix on 2026-09-04 and the reader still
required it, so every bridge check parsed as nothing — `alerts --days 2 --check traefik` printed
"no DOWN alerts" over a window holding 21 `traefik_latency` DOWN lines. `--check` and `--pi` did
not filter `--raw` at all. And a hit `--limit` cut the window at its NEWEST end, so a wider
window could list fewer recent episodes than a narrower one. The emitter/reader pairing is now
enforced by `ansible/tests/services/test_monitor_bridge_down_line_shape.py`, and a truncated
fetch prints its covered window *above* the episode list rather than a warning under an
all-clear.
Two more reader-side gaps closed with #1787: the retry-era `push failed (http=… rc=…)` line
printed its whole scaffolding as the message, and the `push failed transiently` line that
precedes every final failure twice was listed as an episode of its own.

**Episode counts are still a lower bound against Prometheus `monitor_status`**, and that part is
by construction: a push monitor also goes DOWN when its heartbeat expires, which writes no log
line anywhere. Cross-check a flap count against `monitor_status` rather than against this view.

### `arr <app> <api-path>` redacts the credentials it reads

`notification`, `downloadclient`, `indexer` and `importlist` return objects whose
`fields[].value` hold a live credential — the Discord webhook URL, the qbittorrent password,
an indexer API key. The subcommand is read-only against the app, which describes what it does
to the app and not what it does to the transcript: one `arr sonarr notification` put the
`arr_discord_webhook_url` value into an agent transcript on 2026-09-06, and that webhook then
had to be rotated (#1388, rotated by #1389).

`redact_arr_payload` in `probe_lib/arr.py` masks those values with `<redacted>` before
anything is printed, on the `--json` path as well as the pretty-printed one. **Two signals
decide, because neither is enough alone.** The *arr API labels a field's `privacy` as `apiKey`,
`password` or `userName`, but it labels the Discord `webHookUrl` as `normal` — so a name-based
list backs the label up, and a field is redacted as soon as it is *named* like a credential.

Pass `--show-secrets` for the rare case where the value itself is what you need; it prints the
raw response, and what it prints lands in the transcript.

### `monitors` vs `kuma-drift`

`monitors` answers "what is down." **`kuma-drift` answers "what is missing,"** which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up (a fenced-off push tile read green for a day on 2026-08-20).

`kuma-drift` diffs that set against `static-monitors.yaml.j2` and treats a push monitor inside
its own interval after a Kuma restart as pending, since Kuma exports a monitor only once it has
beaten. It reads a templated interval (`{{ uptime_kuma_k8s_bridge_push_interval }}`, the etcd drill's
inventory variable, the UPS tiles' arithmetic) against the role's real render context, so those tiles
are classified by their interval like a literal one — until 2026-09-18 they parsed as None and
could only ever read as missing (#2019).

Pending applies only to a monitor Kuma holds. `kuma-drift` reads which monitors exist from Kuma's
public status page, which `kuma-status-page-sync` keeps listing every declared monitor. A
declared name absent from that page is missing once Kuma has been up for one sync period plus
the sync job's deadline, 1700s, whatever the tile's interval (#4005). A deploy that adds a tile
restarts Kuma, so before that bound a new tile is absent from the page legitimately. Without the page, a
weekly tile AutoKuma refused stayed pending forever, because the weekly reboot restarts Kuma
before the tile's interval elapses. Absent from the page means never created, or added since the
sync last succeeded. The sync runs every 15 minutes, and it fails while any declared tile is
missing from Kuma, so one refused tile also leaves every tile added after it off the page. An
unreadable page keeps the tiles pending and prints a line saying their existence went
unverified.

### The Pi's own first-command triage

daniel-pi runs no kubelet, so `targets`, `kuma-drift`, `alerts` and `health --docker` each need
a `--pi`-shaped answer of their own rather than the cluster's — this is the Docker-plane
equivalent of the four cluster checks above, backed by `probe_lib/pi_plane.py`.

- **`targets --pi`** — Prometheus scrape-target health, scoped to daniel-pi. The declared job
  set (`node-pi`, `alloy-pi`) is parsed from `k8s_pi_client_ip` static targets in observability's
  `prometheus.yaml.j2` rather than hand-listed, so a renamed or added job needs no change here.
  A declared job absent from the live set is reported MISSING and fails the gate — dividing the
  Pi's own live set by itself would repeat `monitors`' N/N-up mistake.
- **`kuma-drift --pi`** — the same declared-vs-live reconciliation, scoped to daniel-pi's own
  monitors. The scope comes from each monitor's YAML `stringData` key in
  `static-monitors.yaml.j2` (`daniel-pi-host.json`, `monitor-bridge-pi.json`, …) carrying `pi`
  as its own hyphen-delimited token — `pihole-k8s-dns.json` is excluded because `pi` there is a
  substring of `pihole`, not a token, and Pi-hole runs on k3s.
- **`alerts --pi`** — scopes the DOWN-episode reconstruction to daniel-pi. The syslog stream
  (the Pi's own health crons) carries the rsyslog host token, verified live against
  `/var/log/pi-health/health.log` (`hostname` there prints `daniel-pi`); the monitor-bridge
  stream carries no host field at all, since it runs in-cluster, so its one check that watches
  the Pi remotely — `pi_pressure` (`check_pi_pressure` in monitor-bridge's `CHECKS`) — is
  matched by name instead.
- **`pi containers`** — one ssh call to daniel-pi (`docker ps -aq` piped into `docker inspect`
  in the same remote shell, so this is a single ssh invocation regardless of container count)
  rendering name/image/status/health/networks for every container on the host. Flags a running
  container with an empty `Networks` map — the reboot-detach failure recorded in
  `containers-lose-network-across-pi-reboot.md` (`Up (healthy)` with no network at all) — and an
  unhealthy healthcheck. A merely stopped container (this host runs the short-lived
  `docker-proxy-lifecycle` sub-proxy) is not flagged; gating on any non-running container would
  read red on a normal day. Measured against the live Pi (2026-09-03, 7 containers, six runs):
  3.65s–19.3s, almost all ssh/exec overhead on a Zero 2 W — `PI_CONTAINERS_TIMEOUT` in
  `pi_plane.py` sits at 45s, above the observed tail rather than at `core.py`'s 10s HTTP
  default, which this call is not.

### `releases [<service>] [--previous] [--json]`

Which commit produced the manifests each k8s service is running. `kubectl` reports what is
running and git reports what is committed; until this existed, nothing joined the two. That join
matters here because `deploy.sh` renders from whatever git tree it is invoked in, so the running
manifests and master can disagree with every repo-side check still green — a worktree 48 commits
behind reverted observability for nine minutes on 2026-08-19, and the only symptom was a
scrape-target count moving.

The records come from `roles/k8s/manifests/tasks/release_stamp.yml`, which writes one JSON file
per service under `/var/lib/homelab/k8s-releases.d/` after every apply. Secret manifests are
listed by name and never hashed.

Two flags carry the finding, and both are normal mid-slice and alarming a week later:

- **`dirty`** — the deploying tree had uncommitted tracked changes, so no commit reproduces
  those bytes.
- **`unmerged`** — the commit is not an ancestor of `origin/master`. A service is running code
  that never landed.

Exit 0 when every record is clean, 1 when any service carries a flag, 2 when no records exist —
which means nothing has been deployed since the stamp shipped, not that the fleet is clean.

`--previous` reads the record kept from before the last deploy. One step of history, not a log:
the incident question is "what was live before this deploy," and depth beyond that is what git
is for, since every record names a commit.

### `health <svc>`

A k8s post-deploy gate. Its argument is a deploy TAG, not a workload name. It gates the
PRODUCTION cluster unless you say otherwise: pass `--cluster prod|stage`, and
`scripts/lib/kubectl.py` refuses when the local kubectl serves a different cluster (#1663).

It exits 0 only when the Deployment **or DaemonSet** is fully rolled out
(observed generation caught up, every replica updated + ready + available) **and** no container
restarted in the last 180s. An unreadable restart time counts as recent, so it fails closed.

Both halves matter — readiness flips a Deployment to Available before a bad liveness probe starts
killing it, so a rollout check alone reports green on a crashlooping pod.

A third half tells a workload that rolled from one that was merely healthy (issue #1867). The
service's release record (`/var/lib/homelab/k8s-releases.d/<service>.json`, written by
`roles/k8s/manifests/tasks/release_stamp.yml` before the restart tasks run) lists each workload
the apply queued a restart of, decided from the same facts the restart tasks read. Every such
workload must carry a `kubectl.kubernetes.io/restartedAt` newer than the record's `applied_at`,
or the gate fails it as `NOT ROLLED`. The pods that were already running satisfy the first two
halves, which is how a deploy that changed the manifests and rolled nothing read `settled`. The
predicate is the record, not the clock, on purpose: an idempotent re-run queues no restart and
stays green, and a standalone `probe.py health` with no record keeps the two-half verdict.

`--docker` inspects the Pi's container over ssh instead. That was the only mode until 2026-08-16,
which is why it died with `FileNotFoundError: 'docker'` on both cluster nodes for the two days
after the Docker retirement.

A role with no Deployment/DaemonSet/StatefulSet but a CronJob — configarr and pi-peer-backup
today — is gated the same way on its most recent Job instead
(`scripts/diagnostics/probe_lib/health_cronjob.py`'s `format_cronjob_health`): the Job must have
succeeded, be newer than the deploy that just ran (read from the `release_stamp.yml` record),
and carry no restarted container. `homelab-readonly`, the identity `probe.py` runs as, cannot
create a Job — verified live with `k3s kubectl auth can-i create jobs`, which the `view`
ClusterRole it is bound to refuses — so this only ever reads the Job `k8s/cronjob-gate` already
created at deploy time, never triggers one. When no Job has landed since the deploy, it falls
back to the CronJob's own daily/weekly schedule: the previous run must have succeeded and not be
more than twice its interval old. Any other schedule shape fails closed rather than guess an
interval. A role with neither a workload nor a CronJob (media-volume, netpol-baseline) still
reports "declares no rollout-checkable workload," which the deploy notifier skips.

### `landing [--json]`

`landing` answers CLAUDE.md's *When to wait* list from a running session. It prints three
things: what would stop a landing now, the `land.sh` and `deploy.sh` runs in flight, and the
worktrees with the issues each has claimed. Exit 1 means something blocks. `--json` prints one
document, which the [deck mod](#the-deck-mod-claudepluginsdeck) reads every minute.

A blocker is one of three states: a non-empty `hold_sha`, a red master CI run, or an owed
`manual_plane` role. The command computes that list itself, so every surface that shows it
agrees. Each fact comes from a reader that already exists:

- Where the running user can read the deployer's state directory, `hold_sha` and the `owed`
  ledger are read from it through `gitops_hold.DeployerSnapshot`. The directory is 0750
  and owned by the deploy user. So off daniel-box, and for the `claude` user on it, deploy-ui's
  `/api/state` serves the hold and the `manual_plane` ledger lines instead.
- A deploy-ui older than the `manual_plane_owed` key leaves the manual planes `null`. A `null`
  field means the source was not readable, not that nothing is owed. It adds no blocker.
- Master CI is the newest `ci.yml` run on master. A run in progress is pending, and a
  conclusion in `deploy_git._CI_NO_VERDICT_CONCLUSIONS` is no verdict. Neither counts as red.
- deploy-ui's `/api/inflight` lists the runs in flight. A run's `VERDICT:` line is read with
  `land_lib.detach.verdict_in` where its log is readable on this host. Elsewhere it comes from
  deploy-ui's `/api/log`, which serves only the logs of runs deploy-ui started. So off
  daniel-box, a terminal run shows no verdict.
- A blocker line carries no age. The mod puts the blockers in the system prompt, and a line
  that changed each minute would rewrite the prompt on every turn.
- `findings.py claims --json` gives the claims. A claim joins a worktree from
  `git worktree list` on its branch, or on the issue numbers a fan-out batch worktree's
  name carries (`fanout-<n>-<n>`). A fan-out batch's issues are claimed under the
  orchestrator's branch, so the branch alone matches none of them. The pane lists a claim no
  worktree works with `findings.py`'s own reason.

A source that fails is named in `errors`, and its field stays `null`. One read takes about 3 s
on daniel-server: one `gh` call, two deploy-ui GETs and one `findings.py` run.


### `gitops-state [--json]`

`gitops-state` prints every marker the GitOps deployer keeps, without ticking (#3931). It reads
`last_run`, `hold_sha` with its `hold_plane` lines, `behind_since`, `diverged_sha`,
`contention_since`, and the `owed` ledger's `manual_plane`, `k8s_deferred` and `k8s_unapplied`
lines. Each set marker carries what discharges it and the command that clears it after. The
hold's command is `gitops_state.py clear-hold <sha>` with the held SHA filled in.
`hold_plane` lines with no `hold_sha` print as orphaned, with `clear-hold --orphaned`.
`gitops_tick.sh` prints this same view after every tick.

It reads `/var/lib/gitops-deploy` directly, so it answers only on daniel-box as the deploy
user. Elsewhere it says there is no state directory. A marker that exists and cannot be read is
reported as unreadable, never as absent. Exit 1 means the directory or a marker could not be
read; the exit says nothing about what the markers hold.
### `ha …`

Reads live Home Assistant state, authed with the SOPS `claude_ha_token`. `ha automation
<id-or-alias>` resolves the alias-slug≠id trap. See the home-assistant role's `CLAUDE.md`.

## `homelab-ui` MCP server

A headless Chromium Claude drives against the LAN routes, so it can *see* a service's UI
(navigate, click, type, accessibility snapshot, screenshot) rather than infer it from a status
code. This is the half `probe.py health` structurally cannot cover: readiness flips a Deployment
to Available while the UI behind it is broken, which is how 19 dead Grafana panels sat behind a
1/1 pod. Registered user-scope, so it is per-operator config rather than a repo file, and
launched by `scripts/diagnostics/ui_mcp.sh`.

### The three things a browser needs here

The wrapper supplies all three.

- **DNS.** This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not resolve to
  the cluster edge from a shell. The wrapper passes Chromium `--host-resolver-rules` pinned to
  the MetalLB ingress VIP — the browser equivalent of the `curl --resolve` pin
  `probe_lib/core.py`'s `k8s_endpoint` documents.
- **Auth.** Every `*.local.<domain>` route is Authelia `one_factor`, so the context loads a
  session cookie minted by `uv run python scripts/diagnostics/ui_login.py`. That login sets
  `keepMeLoggedIn`, which is load-bearing — the session config's `inactivity: '5m'` would
  otherwise expire the cookie between two idle minutes, where `remember_me: '1M'` applies only
  when the login asks for it.
- **Secrecy.** `domain` is SOPS-encrypted, so the config is generated into a 0600 file at launch
  instead of being written into `~/.claude.json`.

`ui_login.py --verify <svc>` proves the cookie reaches the backend without involving the browser,
and it reads a portal 302 as a failure rather than as a reachable service.

### Recovering a stale browser session: `browser_close`, then navigate again

The MCP server reads the state file when it builds a browser context, not on each navigation.
So re-minting the session fixes the FILE and the browser keeps bouncing to the Authelia portal,
because its context still holds the cookie it was built with at launch. That symptom reads
exactly like a route that lost its session middleware, which is the trap worth knowing.

`browser_close` is the reload. It disposes the context, and the next `browser_navigate` builds
a fresh one — which re-reads the state file from disk. Recovering from inside a Claude session
is three steps, and needs no session restart:

```bash
uv run python scripts/diagnostics/ui_login.py            # re-mint
uv run python scripts/diagnostics/ui_login.py --verify homepage   # the file is good
```

then `mcp__homelab-ui__browser_close` followed by any `mcp__homelab-ui__browser_navigate`.

Measured 2026-09-06 against a `playwright-mcp` launched on this host's own config, swapping the
state file underneath it: an empty state at launch landed on the Authelia portal, copying a good
state in and calling `browser_close` landed on the service, and writing the empty state back and
calling `browser_close` again landed on the portal. The file decides, and `browser_close` is what
makes it decide again.

The procedure itself was run end to end on the live server the same day: `browser_close`, then a
`browser_navigate` to `homepage.local.<domain>`, which returned the service's own title rather
than the portal.

The `-m ui` suite is unaffected either way — it launches its own server per run, so it reads the
state file as it stands.

`scripts/diagnostics/tests/test_ui_state_reload.py` is the guard on this procedure, under the
`ui` marker, so a `@playwright/mcp` bump that changed what `browser_close` disposes fails a test
rather than leaving this section quietly wrong. It launches its own server against a private
state file — `UI_MCP_STATE_PATH`, which `ui_mcp.sh` takes in place of the tier's shared jar — so
it can swap the file underneath a running server without touching the session other sessions
read.

### `--check` asks Authelia, never the clock

The expiry stamped in the state file is a claim, and the two come apart in exactly the cases
that matter: restarting Authelia or rotating `authelia_secret` invalidates every live session
while the local timestamp reads valid for weeks.

So `--check` calls `/api/state` and requires `authentication_level >= 1` — Authelia answers HTTP
200 with level 0 to a cookie it no longer honours, so neither the status code nor the timestamp
can stand as the verdict. An unreachable portal counts as invalid: minting needs the same network
the browsing does, so there is nothing useful to do with a session that cannot be confirmed.

### Going through Traefik is not a shortcut, it is the only path

Hitting a ClusterIP directly reaches only pods on the node you run from: the baseline
NetworkPolicy admits the two cni0 gateways alone (`netpol-baseline/defaults/main.yml:41`), and
host-to-remote-node traffic SNATs to flannel.1, which is not listed. `kubectl port-forward` does
not route around it either — the read-only ServiceAccount is denied `create pods/portforward`.

### The regression suite

`uv run pytest -m ui` (`scripts/diagnostics/tests/test_ui_smoke.py`) drives this same MCP server over
stdio, so a break in the wrapper's DNS pin, session minting or launch config fails a test rather
than silently degrading a Claude session. The `ui` marker is deselected by `addopts`, because
these tests need the host's age key, LAN reachability and a browser — none of which a GitHub
runner has.

Pin the **exact** page title when adding a service. Several apps carry their own login behind
Authelia (`FreshRSS` lands on `/i/`, uptime-kuma on `/dashboard`, karakeep on `/signin`), so a
substring like `FreshRSS` also matches `Login · FreshRSS` and scores a broken app green.

**The title is read back after the page settles, not taken from the navigate report.** A
single-page app can pass through a title of its own before applying its configured one —
homepage momentarily reads `Homepage` before `My Awesome Homepage` — and the report captures
whichever moment load-complete caught. Reading once failed a service whose title was correct
half a second later, and looked exactly like a rename.

Two retries in `McpClient` absorb transients rather than reporting them, and
`test_ui_smoke_helpers.py` holds a pass/fail pair for each. `evaluate` retries a reply that
carries the echoed code and no `### Result` block; `settled_title` re-reads the title until
it matches, then returns whatever it last saw so a genuine rename still reaches the
assertion.

### The Grafana panel tier

`test_grafana_dashboard_renders_its_panels` goes one step further for Grafana alone: it logs
in and counts the panels a dashboard actually drew. A title check cannot do that, and the
gap is the reason the tier exists — 19 Angular panels were provisioned to a Grafana that had
dropped Angular and rendered nothing for 55 minutes behind a 1/1 pod.

It authenticates through Authelia. Grafana is an OIDC client of the portal (issue #1374), so
the tier signs out of whatever session the browser profile carried, navigates to
`/login/generic_oauth`, and the Authelia cookie `ui_mcp.sh` already minted completes the
redirect chain. **No credential is typed.** It then asserts `/api/user` reports the Authelia
username, which is the half a 302 cannot prove: the forward-auth middleware redirects before
the backend is reached, so only the logged-in identity shows the OIDC round trip finished.
OIDC login is LAN-only: `root_url` pins the callback to `grafana.local.<domain>`.

**This tier is still how a Claude session verifies a Grafana board.** A
`mcp__homelab-ui__browser_navigate` to `/d/<uid>/` lands on Grafana's own login page — the
admin form stays on as break-glass and as the intended public path — and getting past it by hand
means clicking "Sign in with Authelia" and then re-checking the panels anyway. The tier does
both in Python. Run it instead:

```bash
uv run python scripts/diagnostics/ui_login.py --check   # mint one first if this says expired
uv run pytest -m ui -k grafana                          # ~30s for the five enrolled boards
```

To cover a board that is not enrolled, add its `(uid, min_headers)` to `GRAFANA_DASHBOARDS`
in the same PR that changes the board — the list is deliberately hand-kept, so deriving it
from `files/dashboards/` is not the fix. Pick `min_headers` by enrolling with a deliberately
high number first: the failure names the count observed live (`drew N panel header(s),
expected at least …`), and N is what to pin. Enroll sparingly — the tier opens every board in
one browser, which is what the 2Gi limit below bounds.

`ansible/tests/leakguard.py` exempts the `ui` marker from its PATH shims, because the fixtures
decrypt SOPS before anything renders. Without that exemption every test in this file errors in
setup on `could not decrypt domain`, which reads like a missing age key rather than a stubbed
`sops`.

`grafana_panel_report.classify()` holds the judgement and is unit-tested without a browser.
Three things it separates, each of which cost a debugging session to find:

- **A page that never mounted is retried, never reported.** Its signature is a URL still at
  the bare `/d/<uid>/` — Grafana rewrites it to `/d/<uid>/<slug>` once it has the dashboard —
  or fewer than 10 `data-testid` attributes. Grafana's own chrome is 27 to 53 of them, so a
  testid count alone reads a dashboard-shaped hole as a mounted page.
- **A row-only dashboard passes on rows.** `crowdsec-details-per-machine` is 4 panels, every
  one a `row`; it draws no panel header until a row is expanded.
- **A dashboard that drew *nothing* gets a second load before it is reported.** Measured
  2026-08-30: that same dashboard drew its 12 rows in 2.1s on 6 of 6 isolated loads and drew
  nothing as the fourth dashboard of a run in the same browser. Re-navigating cannot hide a
  real break — an empty dashboard is empty on every attempt. A **partial** render is never
  retried: some panels drawn and some missing is the finding.
- **`No data` is not an error.** Grafana marks an empty panel with the same testid it marks a
  broken one. Failing on the count flags every dashboard whose window is quiet — so the
  message decides. The cost is that a panel whose metric *died* also reads `No data` and
  passes here.

**Rendering these dashboards is expensive server-side.** Opening four of them OOMKilled
Grafana at its old 512Mi limit and again at 1Gi; the working set peaks at ~1084 MiB, and the
limit is now 2Gi. A pod in `CrashLoopBackOff` with nothing but 200s in its log is this, not a
fault — check `Last State` for `OOMKilled`.

### The `two_factor` services

**code-server, n8n and longhorn are `two_factor`**, so the ordinary session bounces off them at
the portal. Launching `ui_mcp.sh --two-factor` mints a second, short-lived session and browses
with it; the `-m ui` tests for those three mint their own the same way. Neither asks for a code.

**That tier logs in as `claude-ui`, not as the operator.** It is an Authelia user that exists
only for the headless browser, and both of its credentials — `authelia_claude_password` and
`authelia_claude_totp_secret` — are SOPS values, so `ui_login.py` derives the code rather than
reading one off a phone. The TOTP registration is seeded into Authelia's SQLite database by the
role's own deploy (`authelia storage user totp generate`), not templated.

Deriving a code means the second factor is another value under the same age key as the first.
The dedicated identity is what makes that trade acceptable: the operator's enrollment is
untouched, revoking Claude's reach into those three services is deleting one block from the
rendered `users_database.yml`, and rotating either credential is a `sops set` plus a deploy.
Until 2026-09-06 the code was typed, and the consequence was that
`test_two_factor_service_serves_its_own_ui` skipped rather than ran — the jar on disk was eight
days stale when this was measured.

`ui_login.py --totp <code>` still accepts a typed code, as break-glass for a seeded secret that
has drifted from Authelia's own row.

The two_factor session also gets its own state file and is never a fallback for the default one:
`ui_mcp.sh` loads a jar unconditionally, so promoting it would put a shell as the repo user
(code-server) and volume deletion (longhorn) behind every page load.

## The deck mod (`.claude/plugins/deck/`)

The deck is a Claude Code mod: a plugin of function hooks, which needs Claude Code 2.1.287 or
newer. It shows the `probe.py landing` snapshot live during a session, where the SessionStart
banner shows that state only once. It reads and gates nothing. A failed read shows in the pane,
and the band and the system-prompt section keep the last good snapshot.

Its three parts:

- **Pane.** `/deck` opens the pane, and `/deck` again closes it. The pane lists the blockers,
  the hold, master CI, the runs in flight, the last landing's `VERDICT:` line, and the
  worktrees with their claims.
- **Band.** A row above the prompt appears only while a landing is blocked. Its Hide button
  turns it off, and `/deck band on` turns it back on.
- **Model context.** While a landing is blocked, a `prompt.compose` hook adds a system-prompt
  section that lists the blockers. `/deck context off` stops it, and `/deck context on`
  restores it.

The mod keeps each toggle in `$.store`, so a toggle survives into the next session. A timer runs
`probe.py landing --json` every 60 s. `/deck refresh` runs it at once. In a checkout without
`scripts/diagnostics/probe.py`, the mod starts no timer.

To load it for one session, start Claude Code with `claude --plugin-dir .claude/plugins/deck`.
A project's settings cannot name a plugin folder. To load it in every session, set
`CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `~/.claude/settings.json`, which the dotfiles
repo owns.

To check a change to the mod, run `claude plugin validate .claude/plugins/deck` and
`claude plugin test .claude/plugins/deck`. CI's `deck_mod` job runs both with the CLI version
that `.github/claude-cli/package-lock.json` pins, and the `prek` gate requires it. The job
checks the mod on a pull request that touches it and on a dispatch, and passes without checking
on a push to master. The test file mocks the probe and runs each drawing on
the `terminal` and `desktop` surfaces. `scripts/diagnostics/tests/test_probe_landing.py` holds
the probe's JSON keys equal to the `DeckSnapshot` fields in `types/index.d.ts`.

## Hooks

Each hook's module docstring under `.claude/hooks/` is the full record of its rules. This
section is the summary a reader needs before opening one.

`run-hook.sh <name> [--project] [--ask-on-cd[=<guards>]]` is the shell entry point every
registration goes through (#3278): one interpreter pin, three postures selected by flags. A
hook's `.py` carries its own `# gen-hooks: register` block, and `args:` there holds the flags.
`scripts/dev/gen_hook_settings.py` renders that block as
`"$CLAUDE_PROJECT_DIR"/.claude/hooks/run-hook.sh <stem> <args>`.

Each session runs the hooks its own checkout carries (#3394). Claude Code sets
`$CLAUDE_PROJECT_DIR` to the directory the session started in, which is the worktree root for
a worktree session. The registrations used to name an absolute path into the primary checkout.
A worktree cut from a fresher `origin/master` then registered a hook script the primary
checkout did not have yet, `/bin/sh` exited 127, and the tool call ran with the guard skipped:
about 2,100 Bash calls did on daniel-server in September 2026 (#2675). The `--project`
posture still runs the hook from the primary checkout's directory, so a fresh worktree needs
no `.venv` of its own.

The two PreToolUse guards, `bash-pretool` and `block-protected-edits`, deny when the runner
itself is gone (#3887). A session outlives its starting worktree's deletion, and `/bin/sh` then
exits 127 on every hook, which the harness treats as non-blocking. One session ran four hours
that way on 2026-10-05 with every guarded call allowed. The generator appends
`scripts/dev/gen_hook_settings.py:GUARD_SUFFIX` to each `--ask-on-cd` registration. That
suffix exits 2 with a stderr reason when `run-hook.sh` is not executable, and passes every
other exit through. The other hooks keep the non-blocking error, because exit 2 on `Stop`
would keep the session working forever.

A worktree cut before #3394 still registers the absolute path. Its hooks run from the primary
checkout until it merges master, and a worktree cut before the #3278 switch still exits 127 on
the six per-hook `.sh` shims #3304 deleted.

### `bash-pretool` (PreToolUse, Bash)

It *decides nothing itself*. It is the one process that runs the four Bash arms —
`block-protected-bash`, `block-footguns`, `inject-nested-docs` and `uv-python` — each of which used to be its own hook with its own `uv run` start. A fifth arm,
`auto-approve-readonly`, moved into the dotfiles `claude_guard` package as `readonly.py`
(dotfiles #628). When #2394 merged them, all five imported `_hook_common` and
`claude_guard.segment`, so four of those five interpreter starts bought nothing: measured on daniel-server, five sequential shims took a median 233 ms against
the dispatcher's 60 ms, and 96 ms when the five ran concurrently (#2394).

Each arm runs under its own `try/except` and contributes a `(decision, reason)` pair or
nothing. `bash-pretool.py` then merges them the way the harness merges separate hooks — `deny`
over `ask` over `allow`, the earliest arm at the winning level keeping the reason — and emits
one `hookSpecificOutput` carrying both that decision and `inject-nested-docs`'s
`additionalContext`. An arm that raises loses its own verdict, keeps the others, and says so
on stderr naming itself.

`uv-python` is the fifth arm since #3286, and the only one that rewrites rather than judges.
It runs LAST, so every decision arm still reads the command the session typed — which is what
the two separate hooks did, `bash-pretool.sh` at order 10 and `uv-python.sh` at order 20. It
was 269 lines of shell, justified by "the interpreter start is the whole cost"; #2394
already pays that start on every Bash call, so a second process bought nothing.

The rewrite rides in the same `hookSpecificOutput` as the verdict. Read from the 2.1.267
bundle: a `deny` drops the `updatedInput` and the call keeps the text as typed, an `ask`
carries it so the prompt is about the rewritten command, and a rewrite with no verdict takes
the plain rewrite path. The harness flattens every PreToolUse hook's output into one
`{deny, ask, allow, updatedInput, additionalContext}` before deciding, so one hook emitting
both keys is indistinguishable from two hooks emitting one each.

A failed `cd` into the repo makes the shim **ask**, naming `block-protected-bash` and
`block-footguns` as the guards that did not run. Three of the five
separate hooks asked in that case before the merge and two stayed silent; one process can only do one thing,
and a missed approval or doc injection is a prompt and a re-read, where a missed deny is a
bypass.

### `block-protected-edits` (PreToolUse, `Edit|Write`)

It *denies* direct edits to SOPS-encrypted files like `ansible/vars/secrets.yml` (use `sops` /
the `/add-secret` skill) and to a generated page, meaning any file carrying a
`generated_from:` banner.

### `block-protected-bash` (a `bash-pretool` arm)

It applies the same two rules on the surface auto mode actually uses. `block-protected-edits`
matches `Edit|Write` only, and auto mode instructs file changes through `sed`, here-documents and
short scripts, so `sed -i … ansible/vars/secrets.yml` reached a bare permission prompt with
nothing saying the file was encrypted. A write here becomes an **ask** carrying `classify()`'s
reason — never a deny, because the path extraction is a heuristic over command text and a wrong
extraction must not block work. Each written path is classified against the checkout that owns
it, as `block-protected-edits` does, not against the session's cwd. Until 2026-10-03 a write by
absolute path into another worktree's generated pages, or by `../` from a subdirectory, was
checked against the wrong tree and passed without a prompt.

It also **denies** a content-printing read (`cat`, `head`, `grep` without `-o`/`-c`/`-l`) of a
deployed host script that renders a credential inline;
`scripts/secrets_mgmt/secret_bearing_host_paths.py` derives that set from the tree.

A Bash write that leaves an isolated session's worktree is **denied** by the dotfiles
`claude_guard` package, not by this hook (`checks/worktree_escape.py`, moved there by #2818).
`isolation-guard.sh` covers `Edit|Write` only, so `cd /home/ubuntu/server && python3 - <<'EOF'`
escaped into the primary checkout and parked the GitOps deployer on 2026-09-06 (#1419). The
check reads nothing this repo owns, so it runs from `guard-pre-tool-use.sh` in every repo.

`block-footguns` splits with the same segment parser through `_hook_common.split_stages` (#2134), so a
newline separates stages for it too, and a here-document body is never one. On text it cannot split,
`block-footguns` asks when the command names a binary one of its rules keys on and stays silent
otherwise. The
allow-side classifier keeps its own splitter: the package splits `cmd &>/dev/null` at the `&`,
which would turn a redirect the classifier allows into a background job it refuses.

### `inject-nested-docs` (a `bash-pretool` arm)

It *adds context* and never makes a decision. A role's `CLAUDE.md` and a `.claude/rules/*.md`
load only when Read/Edit/Write touches a matching path. A `cat`/`sed -n` through Bash — the form
auto mode instructs — loads neither, and 74 of 113 Bash-only session×role pairs never saw the
role doc (measured 2026-09-19, #2125). `.claude/hooks/inject-nested-docs.py` reads the paths a
command names, returns each ancestor `CLAUDE.md` and matching rule as `additionalContext` once
per session, and logs the row to `.claude/logs/instructions.log` as `bash_path_match` so the
same log grades it. Every checkout writes that log in the primary checkout, which
`instructions_log_path` in `.claude/hooks/_hook_common.py` finds through `primary_checkout`,
the parent of `git rev-parse --git-common-dir`. Each
session runs its own checkout's hooks (#3394), and a log in a worktree is deleted with the
worktree. A doc the hook cannot fit arrives as its HEAD up to the budget, then the
headings of the sections the head cut off, then a read pointer: the harness persists a longer
`additionalContext` to disk and hands the model a preview stub instead. The payload is 7,500
chars, and what a doc is weighed against is the 7,272 the preamble leaves, less its own
`===== <doc> (applies to <trigger>) =====` header. A role doc is held under that effective
budget by `ansible/tests/_doc_size.py:MAX_CHARS`, which derives it rather than restating the
7,500 (#3245).

That over-budget form was the heading outline alone until #2650. Sessions read the full doc
after 61 of 262 outline injections (23%), against 69 of 425 Bash-only pairs (16%) before the
hook existed, so the headings bought almost nothing over no injection at all — and the budget
cannot grow, because the remote-control wire path truncates at 8,000 chars / 200 lines. The
head spends the same budget on text a session acts on without a second read: a role doc opens
with its generated `## At a glance` block and its operative rules. It cuts at a heading rather
than mid-section, and the trailer names up to 40 headings it did not reach. Both budgets are
the payload's, not one doc's, so a head that fills the payload defers the next doc to the
next command.

An editing rule arrives with a write, not a read (#2811). Every rule outside `READ_RULES`
(`secrets.md` and `facts.md`, which a read needs) is injected only for a path the command
writes, as `block-protected-bash`'s `written_paths` finds it, or for every named path when the
command runs an inline interpreter such as `python3 -`. Rules were 1.30 MB of the hook's 2.36 MB
in the 7 days to 2026-09-28, spent mostly on reads. They are not dropped from Bash entirely,
because 23% of writes to rule-scoped paths went through Bash that week, and in 5 of 33
session×rule pairs nothing else would have delivered the rule. A rule skipped on a read is not
recorded as injected, so the first write still gets it.

A subagent gets each doc once more. Its payload carries the parent's `session_id`, so the hook
keys its once-only state on the `agent_id` as well. It also tags that subagent's log rows
`agent=<id>`, so a row the subagent caused never suppresses the parent. A small doc that no
longer fits the budget left by earlier docs in the same command waits for the next command.
The hook does not outline it.

A path inside a git object resolves too. `git show origin/master:ansible/roles/k8s/foo/
tasks/main.yml` names a path only after its `<ref>:` prefix is stripped, which the hook missed
until #2651 — a common read form in review sessions and subagents. `path_tokens` emits both
the whole token and the part after its last `:`, and the existence check keeps whichever is
real. A `file:line` token is unaffected: what follows its last `:` is a line number carrying
no `/`.

Re-measured 2026-09-26 (#2192) over the five days after the hook landed, with the same
cross-tab on both sides. The Bash-only no-doc share fell from 85% to 14% in main sessions and
from 82% to 56% in subagents; the subagent gap is what the `agent_id` key closes. The 23%
read-after rate on the outline form is what #2650 replaced. A path named only inside quoted
text or a here-document accounted for 30 of 374 role-doc injections. Some of those were real reads
(`bash -c '…'`, `ssh host '…'`), so the hook does not filter quoted text.

### `block-footguns` (a `bash-pretool` arm)

It *denies* a growing set of commands that return a plausible wrong answer rather than an error,
each with a deterministic signature and a recorded incident. One of them: `kubectl rollout
restart`, which the read-only ServiceAccount is Forbidden from and which prints success anyway.
The docstring of `.claude/hooks/block-footguns.py` is the full list.

Four rules that key on a host tool or on GitHub rather than on this repo moved to the dotfiles
`claude_guard.footguns` module (dotfiles #628), which the user-level PreToolUse hook runs in
every repo: `grep -Z`/`-z` where grep is `ugrep`, a bare `git stash pop`/`apply`, a
self-matching `pgrep -f` and a partial `security_and_analysis` PATCH.

### `session-health` (SessionStart)

On opening a session here, it prints a health banner. It is silent when all-green, read-only
and timeout-bounded. The banner's problem lines come in three groups, in this order:

- **Service lines** (`.claude/hooks/hooklib/service_lines.py`): a down Prometheus scrape target, and
  services whose running release is behind origin/master's manifests.
- **Deployer lines** (`parked_deployer_problems`): a **dirty primary checkout**, a **GitOps
  deployer parked behind origin**, a **setup role the tick merged but cannot apply** (the
  `manual_plane` ledger class), consecutive ticks deferred on a service lock, an image bump
  the tick merged but deferred, and a k8s change the deployer never applies.
- **Branch line**: this worktree is behind origin/master, so a deploy from it would be refused.

A failed import is loud and a failed read is silent. A module the banner cannot import (`lib.git`,
`lib.deployer_park`, `lib.worktrees`) prints a `⚠ … is broken` line, and a failed `hooklib`
import is reported in the fan-out section below. A read that fails at run time, such as a
`git status` timeout or an unreadable marker file, returns no line, so a silent banner is not
proof of health. The release-staleness check is the one read that reports its own failure.

The dirty checkout and the park stop every deploy in the fleet, and a worktree session cannot
look at either for itself: the isolation guard refuses a git command targeting the shared
checkout, and the failure it does see (`deploy.sh` exit 4) names its own tree instead. The
banner is the only place that cause reaches the session that pays for it.

The banner ends with a triage line of whole `probe.py` commands joined by `or`: `targets` and
`health <svc>` always, and `landing` first whenever a deployer line is present.

Three more sections print whether or not anything is unhealthy: the other live Claude sessions
in this repo, merged worktrees ready to remove, and fan-out worktrees running on another host.

### `fanout-stop` (Stop)

It keeps a headless fan-out batch working until the batch names a PR or a blocker. It acts only
in a worktree holding `.fanout/brief.md`, the marker `fanout_lib/launch.py` writes, and prints
nothing everywhere else. There it *blocks* a stop whose final message carries neither a PR URL
nor a line starting `needs input:` or `failed:`, and its reason names the open item. A counter
in `.fanout/stop-blocks` caps it at three blocks per batch.

The cause is how `claude -p` ends. A turn that ends in text with no tool call ends the process,
and Opus writes progress reports (for example, one announcing that the PR comes next) that
sometimes end the turn (issue
#2816). A probe on 2026-09-28 confirmed the mechanism under `claude -p`: the Stop hook fires, and
a `block` continues the session. `fanout_place.py status` reads the same two patterns from the
same final text. A batch reads `done` only with a PR URL, `needs-input` with a blocker line and
`no-pr` otherwise.

## What an edit costs, by file type

This repo now registers one hook on `Edit|Write`: `block-protected-edits`, a ~7 ms no-op except
on the paths it owns. The user-level hooks in the dotfiles repo still run alongside it and are
not counted here.

The two PostToolUse linters that carried this repo's edit cost — `ansible-lint` at **1,642 ms**
on `roles/*/tasks/main.yml` and `validate-compose` at **177 ms** on `docker-compose.yml.j2`,
both measured 2026-08-23 — were deleted in #2856 as duplicates of the prek hooks that run the
same checks at commit time. Over the same 24h the OTEL telemetry put `PostToolUse:Edit` at a
559 ms average and `PostToolUse:Write` at 234 ms. Those figures are history: they are recorded
so a later measurement showing an order-of-magnitude drop reads as the deletion rather than as a
broken exporter.

## `auto-mode-bridge` internals

The two places auto mode and this repo have to talk (`PermissionDenied` + `PostToolUseFailure`,
both Bash).

On a **denial**, it retries `gitops_tick.sh` and nothing else: the tick is allow-listed and still
denied about 1 run in 7 on identical text, which is classifier variance rather than a rule, so
`retry: true` reissues the call and the classifier judges it again. Two retries per session, and
a compound command that merely contains the tick gets none — the classifier judged the whole line.

On a **failure**, it names a `deploy.sh` exit a refusal rather than a playbook failure, and
points at the wrapper's own last two lines for what it was. It decoded the whole exit-code
table until 2026-09-30, as the fifth copy of it; `deploy_run.py:report` now prints the name,
the meaning and the remedy from `scripts/lib/exit_codes.py` on every non-zero exit. What the
hook still holds is the split -- `_REFUSALS` against `_PLAYBOOK_FAILED`, two integers with no
prose, pinned to that module by `test_auto_mode_bridge.py`.

It does **not** use `classifierContext`: that field is PostToolUse-only, so a failed deploy can't
carry one, and the standing facts (public repo, read-only kubectl SA) already live in
`autoMode.environment` and `autoMode.allow`, where the classifier reads them as configuration
rather than as unverified application context.

## Permission auditing

No longer lives here. A `log-permission` hook used to count tool calls and prompts into
`.claude/logs/permissions.json` for `audit-permissions.py` to read; Claude Code's own OTEL
`tool_decision` events carry that now, and name the deciding authority (`config` rule, `hook`,
`user`) instead of leaving it inferred.

Both hosts' Claude Code exports OTLP to their local node's hostPort (127.0.0.1:4317); since
Phase F (2026-08-13) the cluster observability collector is a DaemonSet with a loopback hostPort on
every node, so both hosts reach their own node's collector directly — the Docker forwarder is
dissolved/archived. The reader is the `claude-permission-audit` plugin (`/audit-permissions`),
installed globally rather than vendored per-repo.

**The plugin loads in this repo too.** `.claude/settings.json` disabled it per-project while this
repo kept its own logger and `audit-permissions.py`, because both wrote
`.claude/logs/permissions.json` and running the two double-logged. Retiring the logger removed
that reason, and the entry outlived it until #3142. The plugin registers no hooks of its own: it
ships the skill and the Loki reader, so enabling it here adds `/audit-permissions` and changes
nothing else about a session.
`.claude/hooks/tests/test_project_settings_shape.py::test_no_plugin_is_disabled_here_without_a_reason_on_the_list`
fails if a disable for it comes back without a reason written here.

The events land in **observability's Loki** (`observability` namespace, Service `loki`), not in
`loki-homelab`. `probe.py loki-query` asks `loki-homelab` by default, and until #2210 a
`{service_name="claude-code"}` query there returned a well-formed empty result that read as
"the OTEL stream is gone" while 1.07M lines sat in the other store. A query whose
selector is `service_name="claude-code"` routes to observability's Loki on its own, with a
stderr line saying so; `--loki observability` asks that store for any query, and an explicit
`--loki homelab` with that selector is refused rather than answered empty. `otelq logs` only
ever asks the observability store.

### `/audit-permissions` breaks whenever Loki is not on the node you run it from

And the fix is not in this repo. Its `loki-source.js` hardcodes `LOKI_URL ||
http://127.0.0.1:3100` with no ClusterIP fallback and no retry, reporting "could not read Loki …
Set $LOKI_URL"; `$LOKI_URL` is unset in `settings.json`, the chezmoi base template, the shell rc
files and the plugin's own frontmatter, so the loopback default is what runs.

Loki, Prometheus and Tempo are Deployments with **no `nodeSelector`**, bound to `hostIP:
127.0.0.1` hostPorts — all three sit on daniel-box by scheduler luck, and a reboot can move them.
The 2026-08-23 ClusterIP pin (`c0d8731e`) gave `otelq` and `otel-sweep` a stable second address;
the plugin never got it.

Workaround: `LOKI_URL=http://10.43.99.158:3100`. Durable fix: apply the ClusterIP-fallback pattern
in the `daniel-tools` marketplace repo, which is where the plugin lives — an operator searching
under `ansible/` does not find it (2026-08-23b review M11).

Do **not** pin the workloads to a node; `roles/setup/k3s/defaults/main.yml:893-896` pre-rejects
that — fix the firewall, not the placement. In-cluster consumers (Grafana datasources,
monitor-bridge, autofix-bridge) use Service DNS and are unaffected.
