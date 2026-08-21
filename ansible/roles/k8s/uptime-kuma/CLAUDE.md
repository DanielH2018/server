# uptime-kuma — status monitoring, with AutoKuma reconciling monitors from templates

Uptime Kuma plus an AutoKuma sidecar that creates monitors and notifications from this
role's rendered declarations. See repo-root `CLAUDE.md` for shared conventions.

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
first sync lands, so it needs a startupProbe in front of the liveness probe, or the pod is
killed mid-reconcile.

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
