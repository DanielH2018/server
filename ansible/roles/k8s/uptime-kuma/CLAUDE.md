# uptime-kuma — status monitoring, with AutoKuma reconciling monitors from templates

Uptime Kuma plus an AutoKuma sidecar that creates monitors and notifications from this
role's rendered declarations. See repo-root `CLAUDE.md` for shared conventions.

**Deploy tag:** `--tags "uptime-kuma"`.

## Traps

### AutoKuma 2.0.0 drops resendInterval on push monitors
AutoKuma **v2.0.0** declared `resendInterval` on three monitor variants only — `MonitorHttp`,
`MonitorJsonQuery`, `MonitorKeyword`. `MonitorPush` had no such field, so serde dropped it as
unknown and the value never reached Kuma. The fleet-wide
`kuma_push_resend_interval_minutes: 360` set on 2026-08-16 applied to the 25 http tiles and to
none of the 50 push tiles, which notified once per outage and then stayed silent.

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

Two rc-2 facts the deployment depends on. `@/path` in an env value makes AutoKuma read the
file and strip one trailing newline, so the admin password needs no shell wrapper — which
matters because the rc-2 base is distroless. And `/health` on port 8090 answers 503 until the
first sync lands, so the liveness probe needs an allowance in front of it or the pod is killed
mid-reconcile. That allowance is `initialDelaySeconds: 300` on the liveness probe — see the
next trap for why it is not a startupProbe.

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
