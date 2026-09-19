# uptime-kuma — status monitoring, with AutoKuma reconciling monitors from templates

Uptime Kuma plus an AutoKuma sidecar that creates monitors and notifications from this
role's rendered declarations. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "uptime-kuma"`.
- **Route:** `uptime-kuma.<domain>`, behind Authelia.
- **Claims:** `uptime-kuma-data` and `autokuma-data`, both in the no-backup tier — monitors and
  notifications regenerate from the rendered static-monitors Secret; status history is kept
  nowhere.
- **`k8s_autodeploy: false`** (observability — the alerting spine; a broken deploy cannot page
  about being broken).

## Traps

### AutoKuma 2.0.0 drops resendInterval on push monitors
AutoKuma **v2.0.0** declared `resendInterval` on three monitor variants only — `MonitorHttp`,
`MonitorJsonQuery`, `MonitorKeyword`. `MonitorPush` had no such field, so serde dropped it as
unknown and the value never reached Kuma. The fleet-wide
`kuma_push_resend_interval_minutes: 360` set on 2026-08-16 (renamed `kuma_push_resend_down_beats`
on 2026-09-17 — see the next trap) applied to the 25 http tiles and to none of the 50 push
tiles, which notified once per outage and then stayed silent.

Fixed on **2026-08-21** by taking `ghcr.io/bigboot/autokuma:2.1.0-rc.2` (PR #308). Upstream
moved `resend_interval` into `with_monitor_common_fields_impl!` in 2.1.0-rc.1 (#152), where
every variant carries it. The deploy produced 51 `Updating push:` lines where 2.0.0 produced
zero.

The guard in `test_kuma_static_monitors.py` asserts the rendered template, and the template
was always correct — field spelled right, value right. Nothing between the template and Kuma
was checked, so a discarded setting read as applied at every repo-side gate. A serde model
that ignores unknown fields turns a config typo or a version skew into silence.

The tell is an edit that produces no `Updating <type>:` line in the autokuma sidecar log while
another edit in the same deploy does. Before believing any AutoKuma field is live, check that
it exists on that monitor variant in the pinned tag's `kuma-client/src/models/monitor.rs`.
`test_autokuma_pin_carries_resend_interval_on_push_monitors` enumerates the tags verified that
way and fails when the pin moves off one.

### `resendInterval` counts DOWN beats, not minutes
Kuma re-notifies when a monitor's consecutive `downCount` reaches `resendInterval`
(`server/model/monitor.js`; the push route in `server/routers/api-router.js` does the same),
and its UI labels the field "Resend Notification if Down X times consecutively". The role
carried the value as `kuma_push_resend_interval_minutes: 360` for a month on the reading that
it was six hours. The 3.5-day qbittorrent outage (#1838, 2026-09-13 to 09-16) measured what it
was: the `k3s Workload Health` tile's log showed `Down Count` climbing by 5 every 20 minutes —
one beat per bridge push at 300s plus one per 1200s heartbeat window — and resetting past 360,
so Discord got the down transition and then one resend a day. One of those resends was lost
outright: Kuma logged `Cannot send notification to Homelab Alerts … HTTP 429 Too Many
Requests` at 2026-09-15 14:50, and it does not retry a failed send. Since #1891 monitor-bridge
reads that line out of Loki — and since #1895 the ERROR-level reason line Kuma writes right
after it, so the tile names the status — and pages the **Kuma Notification Delivery** tile, which notifies
email as well as Discord (monitor-bridge/CLAUDE.md). A faster resend raises the POST volume a
long multi-tile outage sends Discord, so the drop it makes likelier is reported rather than
lost.

The variable is `kuma_push_resend_down_beats` now, 90, which is six hours for a bridge-fed tile.
`test_kuma_push_resend_beats.py` derives the spacing from the
bridge's `INTERVAL` and `kuma_bridge_push_interval` and fails outside 4-12h, so a value that
reads as hours again cannot land. The count cannot mean six hours for every tile — a cron-fed
one on a slower cadence resends less often, and a dead bridge leaves only the window's own
beats — which is why the unit is beats and not a time.

Two rc-2 facts the deployment depends on. `@/path` in an env value makes AutoKuma read the
file and strip one trailing newline, so the admin password needs no shell wrapper — which
matters because the rc-2 base is distroless. And `/health` on port 8090 answers 503 until the
first sync lands, so the liveness probe needs an allowance in front of it or the pod is killed
mid-reconcile. That allowance is `initialDelaySeconds: 300` on the liveness probe — see the
next trap for why it is not a startupProbe.

### A pod replacement blinds Prometheus to the push monitors, not Kuma
Minutes after a replacement on 2026-09-11, `probe.py kuma-drift` listed 16 push monitors with
no `monitor_status` series — every `interval: 90000` (25h) tile, `Off-box etcd Snapshot` and
`Secret Rotation` among them — while `probe.py monitors` read "89/89 up" (#1779). The open
question was whether Kuma's down-timer restarts from zero on boot, which would blind
detection for the full interval. It does not, and that is a property of the pinned source, not
of this role.

Read at `louislam/uptime-kuma` tag `2.5.3`, `server/model/monitor.js`, `beat()`:

- **Detection re-arms from the DB.** The `if (!previousBeat || this.type === "push")` branch
  re-reads the newest `heartbeat` row for a push monitor on EVERY cycle, and the push branch
  compares `msSinceLastBeat` against `beatInterval`: a stale last beat throws `No heartbeat in
  the time window` on the first cycle after boot, and a fresh one schedules the next check at
  `interval - msSinceLastBeat`. The heartbeat table is on the persistent PVC, so the clock a
  replacement inherits is the producer's real last push. With `max_retries: 0` an overdue
  producer is DOWN within one cycle of the pod starting.
- **The metric is not re-armed.** That healthy branch ends in `return` at the comment `No need
  to insert successful heartbeat for push type, so end here`, before the
  `this.prometheus?.update(bean, …)` call at the end of `beat()`. So a healthy push monitor has
  no `monitor_status` series until its producer's next push or its timer expires — for a
  daily producer, up to 25h. `master` carries the same `return`.

What this bounds: Kuma's UI and its Discord notifications are DB-backed and see nothing wrong.
Only the readers of `monitor_status` are blind — `probe.py monitors`, `postflight.py`'s Kuma
gate and the uptime-kuma Grafana board. `monitors` prints a coverage line when the exported
set is smaller than the ungated declared one, so the ratio cannot be read as full coverage;
`kuma-drift` already separates a pending tile from a missing one.

Two directions the issue floated, rejected: shortening the 25h intervals buys nothing for
detection (it was never blind) and tightens the dead-man on producers that genuinely run daily;
a Prometheus `absent()` rule has no delivery path here — `prometheus.yaml.j2` records that no
`rule_files` exist, and monitor-bridge is this estate's alerting — and would page after every
replacement for a window in which detection is live.

### A fleet-wide `push failed (http=404)` burst is the edge, not the tokens
`kuma-push-lib.sh` (the host crons' shared pusher) logs `push failed (http=… rc=…)` only after
all three attempts fail. Issue #1803 read 226 such lines carrying `http=404` and `status=up` on
2026-09-06 as Kuma rejecting the pushers' tokens — a monitor set that had moved ahead of, or
behind, the crons — and asked for an ordering constraint. Loki has the cause: Traefik restarted
at 07:41:43 after a host reboot, failed to download the CrowdSec bouncer plugin, and rejected
every router that referenced the `crowdsec` Middleware, so every route on the edge answered 404
until a deploy restarted Traefik at 11:07:41 and the plugin loaded — the `/api/push/` route
included, and the 35 http tiles that went DOWN were right to. The 404s began at 07:42:17; Kuma
itself started at 07:49:45, so the first of them predate the process the issue blamed. The Traefik
startupProbe restart, the `Homelab Edge (all-clear)` tile and monitor-bridge's Traefik 404 Flood
check own that class since the same day, and `#1321` in the issue is a coincidence — it touched
`gitops_deploy` files only.

Every burst of the final-failure line in the 15 days to 2026-09-17 was the same shape, a
fleet-level event another check already pages: 2026-09-05 `http=403` x17 (Authelia answering
while the k3s API was down), 2026-09-06 `http=404` x226, 2026-09-09 `rc=7` x317 (daniel-box down
for 6h). A Kuma-side 4xx on ONE cron's tag while its siblings land is the token class the issue
names, and it has not occurred. So no aggregate alert was added: measured against that
population it would have paged four times for four causes that each paged already, and never
for the class it would exist for. What was added instead (#1869, 2026-09-17) is monitor-bridge's
**Swallowed Push Verdicts**, which pages on a lost `status=down` push only when a sibling cron
landed its push in the same window — the three bursts above have no landed sibling and read as
fleet-wide there, while release-staleness-check's lone `http=500` on 2026-09-10 13:01 and
setup-drift-check's lost DOWN of 2026-08-29 16:47 do page. The census, per host and code:

```
sum by (machine, code) (count_over_time({job="syslog"}
  |~ `push failed \(http=` | regexp `http=(?P<code>\d+)` [1d]))
```

`{job="syslog"}` is the label Alloy puts on these `logger` lines (`config.alloy.j2`); the
transient-retry line reads `push failed transiently` and does not match. Loki's ingest time is
not the event time — the 09-09 burst was shipped in one minute six hours after the lines were
written — so read the timestamp inside the line.

### A startupProbe on the sidecar gated the whole pod's Service
Until 2026-09-06 the allowance above was a startupProbe (`/health`, 30 x 10s). The kubelet holds
`Ready = false` for a container whose startup probe has not succeeded, whether or not that
container has a readinessProbe (`pkg/kubelet/prober/prober_manager.go`, `UpdatePodStatus`:
`if !started { continue }`). Pod Ready is the AND of all containers, so the uptime-kuma Service
had no endpoint until autokuma's first reconcile landed: a measured **76s** of Kuma serving with
nothing routed to it, on every pod replacement, six of them in the 48h to 2026-09-06 (#1348).

Most of that was not reconcile work. autokuma's first connect fails while Kuma is still starting,
kuma-client then waits a hardcoded 9.0s for readiness (`for i in 0..10 { sleep(200ms * i) }` in
`client.rs` at the pinned `v2.1.0-rc.2` — 200 x 45 = 9000ms, matching a measured 9.012s), and the
sync loop sleeps a full `AUTOKUMA__SYNC_INTERVAL` before retrying. **`AUTOKUMA__KUMA__CONNECT_TIMEOUT`
does not shorten it.** The option is real (default 30.0, `kuma-client/src/config.rs`) but that
readiness loop reads no config value at all.

The fix was to delete the startupProbe and move its 300s onto `livenessProbe.initialDelaySeconds`.
Same allowance, same steady-state detection (3 x 30s); the one cost is that a fixed grace is not
adaptive, so a container that never becomes healthy is killed at ~360s rather than ~300s.
**Do not add a readinessProbe here to compensate** — it re-creates the same 76s gap by the same
AND, and it is what `test_readiness_coverage.py` records this container's exemption for.

### AutoKuma compares notification configs by key count
AutoKuma's `config_eq` (`kuma-client/src/models/notification.rs`) compares a notification's
config by counting keys after dropping six ignored ones — `isDefault`, `id`, `active`,
`user_id`, `config`, `name` — and returns false the moment the counts differ. Kuma's `save()`
forces `applyExisting: false` into the stored config on every write
(`server/notification.js`, before the `JSON.stringify`), and that key is not in the ignore
set.

A declaration that omits `applyExisting` is therefore permanently one key short of what Kuma
stores, never compares equal, and is rewritten on every sync pass. That ran from the k3s
cutover to 2026-08-21: 716 rewrites per 30 minutes, ~34k SQLite writes a day onto a Longhorn
volume whose changed blocks the nightly backup then ships. Fixed in PR #309 by declaring the
key; the sidecar went from 10 `Updating notification` lines per 5 minutes to zero `Updating`
lines of any kind.

Raising `AUTOKUMA__SYNC_INTERVAL` from 5 to 60 cut the cost 12x and made the loop look
addressed, which is how it survived a second look. A rate reduction is not a fix — if a
reconciler writes on a pass, the question is why the comparison fails, not how often it runs.

A reconciler that rewrites an unchanged object on every pass has a comparison that cannot
succeed, and the failing key is usually one the server injects rather than one you declared.
Read the server's save path for forced fields before rereading your own template. Do NOT reach
for AutoKuma's debug diff to find it — that prints the whole entity, so for these two objects
it writes the Discord webhook and the SMTP password into Loki. Both sides' source at their
pinned versions answered it with nothing logged.
`test_notification_configs_declare_apply_existing` guards the key.

## One Discord template for every monitor, fed by `description` and tags

The `discord` notification is Kuma's **webhook** provider, not its `discord` one (since
2026-09-18). Both POST to the same Discord webhook; the difference is who writes the body.
The discord provider's own custom template (`discordMessageFormat: custom`) sends plain
`content` and drops the embed. The webhook provider with `webhookContentType: custom` renders
`webhookCustomBody` through the same liquidjs engine (`server/notification-providers/
notification-provider.js`, `renderTemplate`) and posts the result verbatim, so the body is
the embed we author. That body is `files/discord-message.liquid`, embedded into `discord.json`
with `lookup('file') | to_json`. The template belongs to the notification, and every monitor
attaches to that one notification, so one file covers all of them.

What the template reads, per monitor: `heartbeatJSON.msg` (the producer's text — for a
push tile, what the bridge or the cron pushed), `monitorJSON.description` (markdown; Kuma
also renders it at the top of the monitor's own page, the one place its UI renders
formatting), and tags named `severity` and `runbook`, omitted when absent. A tile whose
meaning is not obvious from its name declares a `description` in `static-monitors.yaml.j2`:
what the check reads, what a DOWN means, where to look. Every push tile carries one since
2026-09-19 (#2065); an http tile's URL says what it probes, so those do not.

**Email is the same convention, a second template.** `email.json` renders
`files/email-message.liquid` as `customBody` (plain text — the break-glass tier has to read
in any client, so `htmlBody` stays off) and `[Homelab] {{ status }} {{ name }}` as
`customSubject`, through the same context (#2067).
`ansible/tests/services/test_kuma_email_template.py` renders it the same three ways.

**The two tags are entities this Secret declares** — `tag-severity.json` and
`tag-runbook.json` (#2066). A monitor names one in `tag_names` and AutoKuma resolves the name
to the id it created; `severity: critical` is exactly the email tier
(`test_severity_critical_is_exactly_the_email_tier`), and a `runbook` value is a page the docs
site serves (`test_every_runbook_tag_points_at_a_page_the_docs_site_serves`). The tag
reference is the same NameNotFound hazard as the notification reference: a monitor naming a
tag the Secret no longer declares fails to parse and, under `ON_DELETE=delete`, is deleted
(#2076). `test_every_tag_a_monitor_names_is_a_declared_tag_entity` refuses the typo; it cannot
refuse a deliberate removal of a tag entity while monitors still name it, so remove the
references first and the entity a deploy later. Every reader that classifies entities by
type — the three guards in `test_kuma_static_monitors.py`, `test_status_page_groups.py`,
`status-page-sync-configmap.yaml.j2`'s `index.json` loop and `probe_lib/monitors.py` — skips
`tag` alongside `notification`.

Four things the change depends on:

- **The Liquid body ships inside a Tera `raw` block.** AutoKuma runs every entity through
  the Tera template engine before it parses it (`autokuma/src/entity.rs`,
  `get_entity_from_settings` — unconditional; `files.preprocess` gates only an earlier pass
  over the raw file). Tera reads Liquid's comment tag and its double-brace interpolations as
  its own syntax, so the first deploy of this template on 2026-09-18 failed to parse,
  AutoKuma logged `No notification named discord could be found` for every monitor and
  re-synced all 108 with an empty notification list — every alert detached, behind green
  tiles, until the wrapper deployed. Tera strips the `raw` markers and passes the content
  verbatim. `test_the_notification_ships_this_template_as_a_webhook_body` asserts the
  wrapper. The same parse pass also prints the failing entity's config, webhook URL
  included, into the sidecar log and so into Loki — the debug-diff trap above, reached
  through a WARN line.
- **`webhookAdditionalHeaders` carries `Content-Type: application/json`.** axios posts a string
  body as `application/x-www-form-urlencoded`, and Discord rejects that with a 400.
- **The AutoKuma id stays `discord`** so no monitor's `notification_name_list` moves, and the
  name stays `Homelab Alerts` because monitor-bridge's Kuma Notification Delivery check reads
  it out of Kuma's `Cannot send notification to <name>` line.
- **A Liquid error drops every alert at once.** A parse error throws inside `send()`, Kuma logs
  `Cannot send notification` and does not retry. Kuma Notification Delivery pages on that line,
  and `ansible/tests/services/test_kuma_discord_template.py` renders the file for a DOWN, an
  UP and the Test button's null context before it can deploy. The Test button on its own
  proves nothing: it renders with `heartbeatJSON` null and Liquid renders a missing key as
  empty text.

The test's engine is python-liquid; Kuma's is liquidjs. The template stays in the subset both
accept — `assign x = a == b` is the one divergence found, which is why `down` is set through
an `if` — and the pinned liquidjs rendered the same three contexts to the same payloads on
2026-09-18. The text inside the message is the producer's job: `bridge/msgfmt.py` in
monitor-bridge is the grammar for a multi-item DOWN (group by reason, names once), and
`probe.py releases --stale-only --kuma` is its first cron-side caller.

## The status page's groups are synced by a CronJob, not by AutoKuma

`kuma-status-page-sync` (`templates/status-page-sync-cronjob.yaml.j2`, every 15 min) owns the
group list of the status page at `/status/{{ kuma_status_page_slug }}` — the page Homepage's
`uptimekuma` widget reads. The page OBJECT is hand-created and stays that way; only its
`publicGroupList` is derived.

**Do not declare it as an AutoKuma `status_page` entity, even though the entity type exists.**
`autokuma/src/entity.rs` accepts `"type": "status_page"` and `sync.rs` creates one, but its
`update_entity`'s `match (merge, current)` has arms for Monitor, DockerHost, Notification and
Tag only — a status page falls through to `_ => {}`, at the pinned `2.1.0-rc.2` and on master.
`get_managed_entities` (`autokuma/src/kuma.rs`) does load status pages into the comparison, so
a drifted page logs `Updating status_page` on every pass and writes nothing. And with
`AUTOKUMA__ON_DELETE=delete`, a declaration that ever went away would delete the live page.
`test_no_status_page_is_declared_as_an_autokuma_entity` is the guard.

### Why the join goes through display names
The rules in `kuma_status_page_groups` are written against the AutoKuma id (the declaration's
filename), which is stable and readable. That id is invisible over Kuma's API: `monitor list`
returns display names and numeric ids, and the id-to-name map lives in AutoKuma's own SQLite on
an RWO PVC that nothing else may mount. So `tasks/main.yml` renders `static-monitors.yaml.j2`
back into data under `no_log` and ships `index.json` — id and display name, nothing else — and
the pod resolves name to numeric id at run time. A duplicate display name would silently drop a
monitor from the page, which `test_display_names_are_unique` refuses.

### The sync writes only when the grouping changed
`render_status_page.py` compares group names and their ordered monitor ids against the live
page and writes `desired.json` only on a difference; the apply stage runs `kuma status-page
edit` only when that file exists. Without that, every run would `saveStatusPage` into Kuma's
SQLite — the same Longhorn volume, and the same shape, as the notification-rewrite loop above.
It also starts from the LIVE page and replaces one key, because `saveStatusPage` writes the
whole object and a hand-built document blanks `description`, `theme`, `published` and
`domainNameList`.

### A path-only route loses to a long enough Host() rule
Traefik ranks routers by rule length. `Homelab Edge (all-clear)` probes the edge self-check
path, whose rule `PathPrefix(`/.well-known/traefik-edge-selfcheck`)` is 48 characters — and
littlelink's `Host(`www...`) || Host(`www.local...`)` is 68, so on `www.local` the tile got
littlelink's own 404 body. Measured 2026-09-06, minutes after the deploy that added it.

The probe host is now `edge-selfcheck.local.<domain>`, which fronts no workload and exists only
as a literal `hostAliases` entry on this pod. With no Host() router for that name there is
nothing for the path router to lose to, whatever gets added later.
`test_the_all_clear_probe_host_fronts_no_route` keeps it unrouted and
`test_the_all_clear_probe_host_is_pinned_to_the_ingress_vip` keeps the pin and the tile's URL
in step. The generalisation: **a Kuma tile aimed at a path-only route must use a hostname no
IngressRoute claims**, or its verdict silently becomes a statement about some other app.

### Adding a monitor
Add the declaration as usual. `test_every_declared_monitor_lands_in_a_named_group` fails until
one of the group rules matches its id, so a new tile cannot quietly land in the runtime `Other`
group. `Other` exists so a rule gap on a live cluster still shows the monitor somewhere; the
test keeps it empty in the repo.
