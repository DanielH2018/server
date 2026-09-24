# Claude tooling in this repo — reference

The long form of `CLAUDE.md` → *Claude Tooling in This Repo*. That section keeps one directive
per tool, the part that has to be in context whether or not anyone opens this page. Everything
here is detail you read when you are working on one of these tools, or when one of them has
just surprised you. A hook's own docstring is the fullest record of its rules and incidents,
and a hook that denies prints its reason.

## `scripts/diagnostics/probe.py`

Read-only homelab diagnostics, allow-listed (no prompt). It resolves the live container IP via
`docker inspect`, so prefer it over curling bridge IPs, which change on recreate:

```
uv run python scripts/diagnostics/probe.py <targets | metric '<promql>' | loki-query '<logql>' |
  alerts | monitors | kuma-drift | releases | scrutiny | pi containers | cert <host> | health <svc> |
  ha <state|automation|get> … | b2-spend | longhorn-blocks | vip-placement | readonly-rbac>
```

`uv run python scripts/diagnostics/probe.py --list` prints every subcommand with a one-line
description, sourced from `SUBCOMMANDS`/`REGISTRY` in `probe_lib/subcommands.py` (built on the
shared `scripts/lib/cli_registry.py`, the same named-entry shape monitor-bridge's check
registry uses — see that role's `files/registry.py`). The probe registry is metadata only: `--list` and a completeness
guard (`scripts/diagnostics/tests/test_probe_registry.py`, asserting every `probe_lib` module
with a `run_*`/`main` entry point is covered). Running a subcommand is owned elsewhere and is
unchanged: argparse in `probe_lib/cli_parser.py`, `plan()` in `probe_lib/curl_pipeline.py`, and
the `handlers` table in `probe.py`'s `main()`.

### Measurements and invariant checks: `b2-spend`, `longhorn-blocks`, `vip-placement`, `readonly-rbac`

Four subcommands turn a fact an operator once had to remember into one they can re-derive. Each
has its own test under `scripts/diagnostics/tests/`.

- **`b2-spend [--since 24h]`** sums the Class B spend per volume out of Longhorn's own "changed
  blocks" log lines, because B2 has no usage API (`probe_lib/b2_ledger.py`). It reads Loki
  only and spends nothing on B2. The model covers backups only, so it is a lower bound on the
  console's figure.
- **`longhorn-blocks`** censuses the live Volume CRs by backup tier and block size, and exits 1
  when a `weekly-backup-*` volume is not on 16 MiB blocks (`probe_lib/longhorn.py`).
- **`vip-placement`** reads the Services, EndpointSlices, L2Advertisements and Nodes, and exits
  1 naming any `externalTrafficPolicy: Local` MetalLB VIP with no Ready endpoint on the node
  that announces it (`probe_lib/vip_placement.py`). Host probes stay green through that
  blackout, which is why it exists.
- **`readonly-rbac`** asks live RBAC whether the SA plain `kubectl` runs as still refuses
  `get`/`list` on Secrets and `create`/`delete` on pods, and exits 1 naming any verb it gained
  (`probe_lib/readonly_rbac.py`).

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
beaten. It reads a templated interval (`{{ kuma_bridge_push_interval }}`, the etcd drill's
inventory variable, the UPS tiles' arithmetic) against the role's real render context, so those tiles
are classified by their interval like a literal one — until 2026-09-18 they parsed as None and
could only ever read as missing (#2019).

### The Pi's own first-command triage

daniel-pi runs no kubelet, so `targets`, `kuma-drift`, `alerts` and `health --docker` each need
a `--pi`-shaped answer of their own rather than the cluster's — this is the Docker-plane
equivalent of the four cluster checks above, backed by `probe_lib/pi_plane.py`.

- **`targets --pi`** — Prometheus scrape-target health, scoped to daniel-pi. The declared job
  set (`node-pi`, `alloy-pi`) is parsed from `k8s_pi_client_ip` static targets in claude-otel's
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
behind reverted claude-otel for nine minutes on 2026-08-19, and the only symptom was a
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

## Hooks

Each hook's module docstring under `.claude/hooks/` is the full record of its rules. This
section is the summary a reader needs before opening one.

### `bash-pretool` (PreToolUse, Bash)

It *decides nothing itself*. It is the one process that runs the five Bash arms —
`auto-approve-readonly`, `block-protected-bash`, `nudge-land-sh`, `block-footguns` and
`inject-nested-docs` — each of which used to be its own hook with its own `uv run` start. All
five import `_hook_common` and `claude_guard.segment`, so four of those five interpreter starts
bought nothing: measured on daniel-server, five sequential shims took a median 233 ms against
the dispatcher's 60 ms, and 96 ms when the five ran concurrently (#2394).

Each arm runs under its own `try/except` and contributes a `(decision, reason)` pair or
nothing. `bash-pretool.py` then merges them the way the harness merges separate hooks — `deny`
over `ask` over `allow`, the earliest arm at the winning level keeping the reason — and emits
one `hookSpecificOutput` carrying both that decision and `inject-nested-docs`'s
`additionalContext`. An arm that raises loses its own verdict, keeps the other four, and says
so on stderr naming itself.

`uv-python.sh` stays a separate hook: it rewrites the command rather than judging it, so it has
no verdict to merge. `bash-pretool.sh` runs ahead of it, which is where `auto-approve-readonly`
already sat, so every arm reads the command the session typed.

A failed `cd` into the repo makes the shim **ask**, naming `block-protected-bash`,
`nudge-land-sh` and `block-footguns` as the guards that did not run. Three of the five arms
asked in that case before the merge and two stayed silent; one process can only do one thing,
and a missed approval or doc injection is a prompt and a re-read, where a missed deny is a
bypass.

### `block-protected-edits` (PreToolUse, `Edit|Write`)

It *denies* direct edits to (a) anything under `containers/` (edit the
`ansible/roles/containers/<svc>/templates/` source instead) and (b) SOPS-encrypted files like
`ansible/vars/secrets.yml` (use `sops` / the `/add-secret` skill). It also denies a write to a
generated page, meaning any file carrying a `generated_from:` banner.

### `block-protected-bash` (a `bash-pretool` arm)

It applies the same two rules on the surface auto mode actually uses. `block-protected-edits`
matches `Edit|Write` only, and auto mode instructs file changes through `sed`, here-documents and
short scripts, so `sed -i … ansible/vars/secrets.yml` reached a bare permission prompt with
nothing saying the file was encrypted. A write here becomes an **ask** carrying `classify()`'s
reason — never a deny, because the path extraction is a heuristic over command text and a wrong
extraction must not block work.

It also **denies** a content-printing read (`cat`, `head`, `grep` without `-o`/`-c`/`-l`) of a
deployed host script that renders a credential inline;
`scripts/secrets_mgmt/secret_bearing_host_paths.py` derives that set from the tree.

A third arm **denies** a Bash write that leaves an isolated session's worktree.
`isolation-guard.sh` covers `Edit|Write` only, so `cd /home/ubuntu/server && python3 - <<'EOF'`
escaped into the primary checkout and parked the GitOps deployer on 2026-09-06 (#1419). The arm
carries the `cd` through the command a segment at a time, and is inert outside a
`.claude/worktrees/` session. It denies where a protected-file write only asks, because a write
outside the worktree is never the right call.

The segments are the dotfiles package's (`claude_guard.segment.parse`, #2053), so a quoted `;`
stays inside its word. Text it cannot split — no `claude_guard` deploy on the host, or an
unbalanced quote — is an **ask** naming the fix, never a silent pass. `block-footguns` and
`nudge-land-sh` split with the same segment parser through `_hook_common.split_stages` (#2134), so a
newline separates stages for them too, and a here-document body is never one. On text it cannot split,
`block-footguns` asks when the command names a binary one of its rules keys on and stays silent
otherwise; `nudge-land-sh` stays silent, since a missed nudge costs one hand-written poll. The
allow-side classifier keeps its own splitter: the package splits `cmd &>/dev/null` at the `&`,
which would turn a redirect the classifier allows into a background job it refuses.

### `inject-nested-docs` (a `bash-pretool` arm)

It *adds context* and never makes a decision. A role's `CLAUDE.md` and a `.claude/rules/*.md`
load only when Read/Edit/Write touches a matching path. A `cat`/`sed -n` through Bash — the form
auto mode instructs — loads neither, and 74 of 113 Bash-only session×role pairs never saw the
role doc (measured 2026-09-19, #2125). `.claude/hooks/inject-nested-docs.py` reads the paths a
command names, returns each ancestor `CLAUDE.md` and matching rule as `additionalContext` once
per session, and logs the row to `.claude/logs/instructions.log` as `bash_path_match` so the
same log grades it. A doc over 7,500 chars arrives as its heading outline plus a read pointer:
the harness persists a longer `additionalContext` to disk and hands the model a preview stub
instead.

### `nudge-land-sh` (a `bash-pretool` arm)

It *denies* a command that blocks on CI (`gh run watch`, `gh pr checks --watch`) and the third or
later CI-status read in one session, naming the `land.sh --pr <n> --since <sha>` form instead.
The first two reads are an ordinary glance and pass. Measured over the 7 days to 2026-08-29: 173
`gh pr checks` + 75 `gh run list` + 61 `gh run watch` against 29 `land.sh` runs, which is why the
CLAUDE.md paragraph became a hook.

### `block-footguns` (a `bash-pretool` arm)

It *denies* a growing set of commands that return a plausible wrong answer rather than an error,
each with a deterministic signature and a recorded incident. Two of them: `grep -Z`/`-z` (this
host's grep is `ugrep`, where those mean `--fuzzy` and `--decompress`, not the NUL flags — use
`--null`/`--null-data`), and a bare `git stash pop`/`apply` (the stash stack is per-repository,
so it can take another session's WIP). The docstring of `.claude/hooks/block-footguns.py` is the
full list.

### `validate-compose` (PostToolUse)

It re-renders all compose templates after you edit a `docker-compose.yml.j2`, an
`ansible/templates/*.j2` macro, or `host_vars`/`group_vars/all.yml`. It fails on malformed YAML,
which catches Jinja indent bugs `ansible-lint` misses. It also fails on an un-escaped `$` in a
`command`/`entrypoint`/`healthcheck.test`: Compose interpolates a lone `$VAR`/`$(…)` at parse
time, so shell `$` must be doubled `$$`. A legitimate `${VAR-…}` in `environment:` is not
flagged.

### `session-health` (SessionStart)

On opening a session here, it prints a banner of any unhealthy or restarting containers and down
Prometheus targets. It is silent when all-green, read-only and timeout-bounded. It also names a
**dirty primary checkout**, a **GitOps deployer parked behind origin**, and a **setup role the
tick merged but cannot apply** (the `manual_plane` marker). The first two states stop every
deploy in the fleet, and a worktree session cannot look at either for itself: the isolation
guard refuses a git command targeting the shared checkout, and the failure it does see
(`deploy.sh` exit 4) names its own tree instead. The banner is the only place that cause
reaches the session that pays for it.

## What an edit costs, by file type

Six hooks match `Edit|Write`, and each is a ~7 ms no-op except on the paths it owns. Measured
directly 2026-08-23, one payload per hook: `ansible-lint` takes **1,642 ms** on
`roles/*/tasks/main.yml` and 7 ms on a `.j2` manifest, a Compose template or a Markdown file;
`validate-compose` takes **177 ms** on `docker-compose.yml.j2` and 7 ms on everything else.

Over the same 24h the OTEL telemetry put `PostToolUse:Edit` at a 559 ms average and
`PostToolUse:Write` at 234 ms — the two populations differ in which file types they touch, not in
which hooks run. So editing a tasks file is the slow case by an order of magnitude, and that is
the price of the coverage rather than overhead to trim. Recorded so a slow-feeling edit isn't
mistaken for a stuck hook.

## `auto-mode-bridge` internals

The two places auto mode and this repo have to talk (`PermissionDenied` + `PostToolUseFailure`,
both Bash).

On a **denial**, it retries `gitops_tick.sh` and nothing else: the tick is allow-listed and still
denied about 1 run in 7 on identical text, which is classifier variance rather than a rule, so
`retry: true` reissues the call and the classifier judges it again. Two retries per session, and
a compound command that merely contains the tick gets none — the classifier judged the whole line.

On a **failure**, it decodes `deploy.sh` exits 75/4/3/2 into what each one means, because all four
mean *nothing was deployed* and they reach Claude as a bare `Exit code N` that reads like a
playbook failure. `test_auto_mode_bridge.py` pins its `_DEPLOY_EXITS` table to
`scripts/deploy_tools/exit_codes.py`.

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
Phase F (2026-08-13) the cluster claude-otel collector is a DaemonSet with a loopback hostPort on
every node, so both hosts reach their own node's collector directly — the Docker forwarder is
dissolved/archived. The reader is the `claude-permission-audit` plugin (`/audit-permissions`),
installed globally rather than vendored per-repo.

The events land in **claude-otel's Loki** (`observability` namespace, Service `loki`), not in
`loki-homelab`. `probe.py loki-query` asks `loki-homelab` by default, and until #2210 a
`{service_name="claude-code"}` query there returned a well-formed empty result that read as
"the OTEL stream is gone" while 1.07M lines sat in the other store. A query whose
selector is `service_name="claude-code"` routes to claude-otel's Loki on its own, with a
stderr line saying so; `--loki claude-otel` asks that store for any query, and an explicit
`--loki homelab` with that selector is refused rather than answered empty. `otelq logs` only
ever asks the claude-otel store.

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
