# monitor-bridge — every threshold check, pushed to Uptime Kuma (k8s)

A stdlib-only Python loop (`files/cli.py`) that runs every registered check each `INTERVAL`
(300 s) and pushes `status=up|down&msg=…` to one Uptime Kuma **push** monitor per check, so a
threshold breach pages. The *At a glance* block below has why it carries neither a readiness
probe nor auto-deploy.

**`files/check_table.py`'s `CHECKS` declares every check once**: body, push token, gate and
Kuma tile, from which the registry, the gate sets, the env-secret and uptime-kuma's tiles
derive. Each check's `verdicts/` function says what it evaluates. This file states what holds across every check.
`docs/monitor-bridge-checks.md` has the measurement behind each number, and
`docs/monitor-bridge-internals.md` the module table, prerequisites, gate-set membership and
test-patching rules.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "monitor-bridge"`
- **Image:** `python` (`monitor_bridge_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — this IS the alert
  pipeline; a broken deploy cannot page about being broken. ALSO no readinessProbe (probe-less)
  — two independent reasons
<!-- /generated_from -->

**Host:** daniel-box, pinned by `nodeSelector` because the gitops checks read the deployer's
state directory over a hostPath. Every source is reached by in-cluster Service name, so no VIP,
Traefik or gate sits in a probe's path.

## Gates and hysteresis

Four reachability gates run first each cycle and suppress their dependents, so one outage pages
once; a suppressed check pushes `up` with a `skipped — <source> unreachable` message, so its
heartbeat survives. A check's row names its gate in the `gate` column, with the reason, and
`files/gates.py` derives the sets from it.

- **Prometheus, Loki, B2 and WAN Reachable** suppress the rows naming them. One value per row,
  a gate or `startup_grace`, keeps the sets disjoint. The internals page has the membership
  rules.
- **A gate has no gate of its own** — a tile that cannot go red is not coverage; #2825 deleted
  the second Prometheus gate on that reasoning.
- **`EXPORTER_DEPENDENT`** suppresses a dead node-exporter's dependents, keyed by scrape job,
  so one host's dead exporter holds them estate-wide (`DECIDED:` at the probe in
  `files/check.py`; the internals page has the membership). A new scrape job fails
  `test_every_node_exporter_job_is_mapped_in_exporter_dependent`.
- **`STARTUP_GRACE` and the `*_CONSECUTIVE` streaks** (`bridge/streaks.py`) are the per-check
  hysteresis: a held cycle pushes `up` with a `down streak n/N` note, so a streak delays a page,
  never suppresses one.

## Checks

**Per-check rules are not restated here** — read the registry entry, its `verdicts/` function,
and the checks page for why a number is what it is. Three conventions hold across all of them:
an empty credential or URL disables a check, which stays `up` rather than paging about its own
configuration; an unreachable source pages through `_evaluate` unless the check names a streak;
and every threshold, window and streak is an env var in `templates/env-secret.yaml.j2`, so read
the template rather than the constant.

Two push-side rules bind as widely. **Every push monitor has `max_retries=0`**, so widen
`uptime_kuma_k8s_bridge_push_interval` to fix post-boot flapping and never add retries
(`test_push_monitors_never_retry`). **A new arm folds into an existing monitor by default**;
prove its selector against a live source first (checks page's *Traps*). **A new check is one
`check_table.py` row, one `check_*` body and its push-token secret** (`/add-secret`); the
Kuma tile renders from the row, and landing it deploys uptime-kuma too. The liveness probe,
the credentials and the prerequisites are on the internals page.

## Module layout — and the one rule that governs it

`files/` holds its flat modules and three packages: `bridge/` (shared plumbing), `checks/` (one
module per domain of `check_*` bodies, mirroring its test file) and `verdicts/` (pure logic
taking its inputs as arguments). `check_table.py` imports only `bridge.types` and the
`checks.*` bodies; `registry.py` and `gates.py` import it, never each other. A row's fields
but `fn` are literals: `ansible/filter_plugins/py_table.py` parses the table without running
it. Modules split at 600 lines; the internals page has the per-module table.

**Adding a module means adding its path to `monitor_bridge_modules`** in `defaults/main.yml`, a
flat ConfigMap key mounted back at its path by the Deployment's `items:`. A module missing from
it kills the pod at import on its next roll;
`ansible/tests/services/test_monitor_bridge_modules.py` and its mount-layout sibling catch that.

## Configuration is a parameter, not a module global

`cli.py`'s `main()` calls `load_config(os.environ)` ONCE and hands the frozen `Config` to
`check.run_once`, which passes it to every gate, check body and `bridge.net` helper that reads a
URL; a test states the registry, the `Gates` and its I/O (a `FakeSources` as `src`).
Building the config MUST NOT raise: `_int`/`_num` record a malformed or missing value in `CONFIG_PROBLEMS`, and
`main()` exits 2. **A key the env-secret renders has no Python default**; the `cfg` fixture is
that render. A default argument cannot read the config (it evaluates at import), and a
`verdicts/` module reads no `cfg` at all. The internals page has the test-side rules.

## Editing & testing
Unit tests are `uv run pytest ansible/roles/k8s/monitor-bridge/tests`, one file per domain.
**A shared test helper goes under `tests/`, never beside `cli.py`** — every `.py` in `files/` is
production code to `_runtime_modules()`. Smoke test one pass with
`sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once`;
`--dry-run` pushes nothing, and `--check <name>` refuses a gated check without its gate.

## Traps

Each came from an incident; the checks page's *Traps* section holds the evidence.

- **kube-state-metrics sanitizes resource names into labels:** `devic.es/dri` arrives as
  `resource="devic_es_dri"`. Sanitize at query time (`ksm_resource_label`) and name both forms
  in the message.
- **The Alloy k8s pod streams have no `app` label** — they carry `container` / `pod` / `job` /
  `namespace` / `service_name`, so a selector written from the `-l app=` habit matches nothing
  and a fail-open arm reads that as health. `LOKI_STREAM_LABELS` and
  `test_loki_selectors_use_real_stream_labels` pin the vocabulary.
- **The runtime stamps the log lines, not `bridge.common.log`** — read a `kubectl logs
  --timestamps` prefix as UTC.
