# monitor-bridge — every threshold check, pushed to Uptime Kuma (k8s)

A stdlib-only Python loop (`files/cli.py`) that runs every registered check each `INTERVAL`
(300 s) and pushes `status=up|down&msg=…` to one Uptime Kuma **push** monitor per check, so a
threshold breach pages. The *At a glance* block below has why it carries neither a readiness
probe nor auto-deploy.

**`files/registry.py`'s `build_checks()` is the authority on which checks exist**; each check's
`verdicts/` function says what it evaluates. This file states what holds across every check.
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
heartbeat survives. The sets live in `files/gates.py`, pinned to the registry.

- **Prometheus Reachable**: when Prometheus is unreachable, every
  prom-dependent check (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/traefik_latency/traefik_404/traefik_421/ups/
  host_temp/shipper_dropped/longhorn_volumes/snapshot_headroom/kubelet_plugin_readonly/pi_pressure/
  k8s_workloads/cluster_targets/pvc_fullness/etcd_db_size) is
  **suppressed**. `tests/test_claude_md_prom_dependent_enumeration.py` pins that list to
  `PROM_DEPENDENT` — edit the set and the sentence together.
- **Loki, B2 and WAN Reachable** gate their own dependent sets.
  `docs/monitor-bridge-internals.md` has each membership and the rules that shaped it.
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
prove its selector against a live source first (checks page's *Traps*). The push tokens, the
liveness probe, the credentials and the prerequisites are on the internals page.

## Module layout — and the one rule that governs it

`files/` holds four flat modules and three packages: `bridge/` (shared plumbing), `checks/` (one
module per domain of `check_*` bodies, mirroring its test file) and `verdicts/` (pure logic
taking its inputs as arguments). `registry.py` and `gates.py` import `bridge.types` and the
`checks.*` bodies, never each other. Modules split at 600 lines; the internals page has the
per-module table.

**Adding a module means adding its path to `monitor_bridge_modules`** in `defaults/main.yml`, a
flat ConfigMap key mounted back at its path by the Deployment's `items:`. A module missing from
it kills the pod at import on its next roll;
`ansible/tests/services/test_monitor_bridge_modules.py` and its mount-layout sibling catch that.

## Configuration is a parameter, not a module global

`cli.py`'s `main()` calls `load_config(os.environ)` ONCE and hands the frozen `Config` to
`check.run_once`, which passes it to every gate, check body and `bridge.net` helper that reads a
URL; a test states the registry and the `Gates` rather than patching tables. Building the config
MUST NOT raise: `_int`/`_num` record a malformed or missing value in `CONFIG_PROBLEMS`, and
`main()` exits 2. **A key `templates/env-secret.yaml.j2` renders has no Python default** — the
template holds its value once, and the `cfg` test fixture is that render (#3659). A default
argument cannot read the config — defaults evaluate at import — and a `verdicts/` module reads
no `cfg` at all. The internals page has the test-side rules.

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
- **Promtail's k8s streams have no `app` label** — they carry `container` / `pod` / `job` /
  `namespace` / `service_name`, so a selector written from the `-l app=` habit matches nothing
  and a fail-open arm reads that as health. `LOKI_STREAM_LABELS` and
  `test_loki_selectors_use_real_stream_labels` pin the vocabulary.
- **The runtime stamps the log lines, not `bridge.common.log`** — read a `kubectl logs
  --timestamps` prefix as UTC.
