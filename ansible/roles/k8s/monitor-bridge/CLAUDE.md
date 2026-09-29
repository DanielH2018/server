# monitor-bridge — every threshold check, pushed to Uptime Kuma (k8s)

A stdlib-only Python loop (`files/cli.py`) that runs every registered check each `INTERVAL`
(300 s) and pushes `status=up|down&msg=…` to one Uptime Kuma **push** monitor per check, so a
threshold breach pages. It is the alert pipeline: it has no readiness probe and is denylisted
from auto-deploy, because a broken deploy cannot page about being broken.

**`files/registry.py`'s `build_checks()` is the authority on which checks exist**, and each
check's `verdicts/` function is the authority on what it evaluates. This file carries what is
true of every check — the gates, the hysteresis, the module layout and the editing rules — and
points at those two for the per-check rule. The measurements, incidents and retirements behind
every number are in `docs/monitor-bridge-checks.md`; read that when you need to know *why* a
threshold is what it is.

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

- **Stdlib only** (no build, no extra deps) · **No web UI** · **Depends on:** prometheus
- **Host:** daniel-box — pinned by `nodeSelector`, because the gitops checks read the
  deployer's state directory over a hostPath.
- **Reaches:** every source over its in-cluster Service name (`templates/env-secret.yaml.j2`)
  — no VIP, no Traefik, no gate in a probe's path. speedtest needs its own NetworkPolicy
  (`netpol-baseline`'s `networkpolicy-speedtest.yaml.j2`).

## Gates and hysteresis

Four reachability gates run first each cycle; each suppresses its dependents so one outage
pages once. A suppressed check pushes `up` with a `skipped — <source> unreachable` message so
its heartbeat stays alive. The sets live in `files/gates.py`, each pinned to the registry.

- **Prometheus Reachable** (`vector(1)`): when Prometheus is unreachable, every
  prom-dependent check (disk/cert/memory/restarts/oom/cpu/targets/traefik5xx/traefik_latency/traefik_404/traefik_421/ups/
  host_temp/shipper_dropped/longhorn_volumes/snapshot_headroom/kubelet_plugin_readonly/pi_pressure/
  k8s_workloads/cluster_targets/pvc_fullness/etcd_db_size) is
  **suppressed**. `tests/test_claude_md_prom_dependent_enumeration.py` pins that list to
  `PROM_DEPENDENT`; edit the set and the sentence together.
- **Loki Reachable** (`/loki/api/v1/labels`): gates `LOKI_DEPENDENT` — `loki_ingestion`,
  `swallowed_verdicts`, `kuma_notify_failures`. `ha_heartbeat` is deliberately NOT a member:
  its ban arm fails open on a Loki error so the heartbeat verdict survives.
- **B2 Reachable** (`b2_authorize_account`): gates `B2_DEPENDENT` (`b2_storage`). One probe per
  `B2_PROBE_INTERVAL_S` (1800 s) with a BILLED outcome cached, because the fault it detects is a
  transaction cap; a failure that never reached B2 takes the short `B2_TRANSPORT_RETRY_S` TTL.
- **WAN Reachable** (two provider URLs, by hostname): gates `WAN_DEPENDENT` — `r2_usage`,
  `cloudflare_ips_drift`, `healthchecks_drift`, `discord`. DOWN only when NEITHER answers, so
  one provider's outage cannot silence a dependent reading the other. Never an anycast IP: the
  2026-09-18 outage included DNS failure. `b2_reachable` is the tile's PEER, not a member — it
  is a gate itself, and this loop has no gate-of-a-gate.
- **There is no second Prometheus gate.** `Cluster Prometheus Reachable` gated a
  `CLUSTER_DEPENDENT` set reading `CLUSTER_PROMETHEUS_URL` until 2026-09-28. Both URLs named
  one cluster Service after the Docker plane retired (2026-08-14), so its tile could not go
  red on its own and read as coverage that did not exist; #2825 folded its four members into
  `PROM_DEPENDENT` and deleted the gate, the URL, the push token and the tile. Reintroducing a
  second Prometheus is a `git revert`, and a check reading one needs a gate watching it.
- **`EXPORTER_DEPENDENT`** maps a node-exporter scrape job to the checks a dead exporter would
  otherwise page twice: `node` → disk, memory, host_temp; `node-pi` → host_temp, pi_pressure.
  `pvc_fullness` gets NO entry keyed on the kubelet job on purpose — its claim-count floor
  exists to page on that partial outage. A new node-exporter scrape job fails
  `test_every_node_exporter_job_is_mapped_in_exporter_dependent` until it is mapped. The probe
  reads a BARE `up`, never `origin_sel()` — the pin left node-pi and daniel-box unreachable
  (#2779) — and suppression is keyed by job, so one host's dead exporter holds its dependents
  estate-wide (`DECIDED:` at the probe in `files/check.py`).
- **`STARTUP_GRACE`** (`n8n`, `bazarr`, `prowlarr_indexers`, `scrutiny`, `speedtest`) holds a
  reach-out check with no gate and no streak of its own `up` for the first `GRACE_CYCLES`-1
  consecutive down cycles, so the weekly Sunday reboot's first cycle does not page. Disjoint
  from every skip set, so a check that gains a gate leaves this set; a test holds both.
- **Consecutive-cycle streaks** (`bridge/streaks.py`, every `*_CONSECUTIVE` in cycles of
  `INTERVAL`) are the per-check hysteresis. A held cycle pushes `up` with a `down streak n/N`
  note, because a monitor that is up while a fault accumulates has to say so. A streak delays
  a page; it never suppresses one.

## Checks

**Per-check rules are not restated here.**
`ansible/roles/k8s/monitor-bridge/files/registry.py`'s `build_checks()` names every check that
exists, each one's `verdicts/` function carries its thresholds and arms, and
`docs/monitor-bridge-checks.md`'s *Live checks* walks them in registry order with the
measurement behind every number. Read the registry entry and its verdict function for the
rule; read that page for why the number is what it is.

Three conventions hold across all of them, so a new check follows them without being told:

- An empty credential or URL disables a check, which then stays `up` rather than paging about
  its own configuration.
- An unreachable source pages through `_evaluate` unless the check names a streak, and a gate
  suppresses it instead when the whole source is down (*Gates and hysteresis*).
- Every threshold, window and streak is an env var rendered in
  `templates/env-secret.yaml.j2`, so the deployed value can differ from the in-code default.
  Read the template, not the constant, when you need the live number.

## Push-monitor mechanics

- **Every push monitor has `max_retries=0`** so the bridge's own `down` push flips the state
  and the descriptive message reaches Discord. Post-boot flapping is fixed by widening the
  heartbeat window, never by adding retries (`test_push_monitors_never_retry` is the guard).
- **The heartbeat window is `kuma_bridge_push_interval` = 1200 s**, 4 × the loop.
- **Liveness probe:** `cli.py` touches `/tmp/heartbeat` after every cycle and the probe in
  `templates/deployment.yaml.j2` fails past ~3×INTERVAL, so the kubelet restarts a hung loop.
- **Push tokens:** `templates/env-secret.yaml.j2`'s `KUMA_PUSH_*` keys are the list, one
  SOPS `monitor_bridge_<check>_push_token` each; `test_every_push_token_env_is_wired_to_a_monitor`
  pins them to the AutoKuma monitors and `test_checks_and_env_secret_push_tokens_agree` to the
  registry. Count them with `grep -c '^\s*KUMA_PUSH_[A-Z0-9_]*:'` on that template rather
  than trusting a number written here.
- **Folding an arm into an existing monitor is the default** over a new tile, which costs a
  new push token in SOPS and a monitor created by hand. Fold when the arm answers the tile's
  existing question (cgroups into Memory, ports into Pi Pressure, ip_ban into HA).
- **A new arm ships with its selector run against the live source over a window holding a
  KNOWN event.** Unit tests mock the payload and prove the verdict, never the selector; a
  fail-open arm goes green on a typo and a fail-closed one pages on it (*Traps*).
- **File-mounted credentials** (`HA_TOKEN_FILE`, `B2_PROBE_APPLICATION_KEY_FILE` reusing
  `longhorn_b2_application_key`, `CF_ANALYTICS_TOKEN_FILE`, `SPEEDTEST_TOKEN_FILE`,
  `HEALTHCHECKS_API_KEY_FILE`) are rendered 0600 and read through
  `bridge.config._env_file`; an empty file disables the check. Ids stay inline.
- **The gitops checks read `/var/lib/gitops-deploy` over a `:ro` hostPath** the
  `gitops_deploy` role creates: deploy that role before this one on a fresh host.
- Thresholds are env-tunable in `templates/env-secret.yaml.j2`; `bridge/config*.py` names the
  default for each. A failed query makes that monitor `down` with an explanatory msg.

## Operator prerequisites
1. A push token in `secrets.yml` for every `KUMA_PUSH_*` key in `templates/env-secret.yaml.j2`
   — exactly 32 alphanumeric chars (`openssl rand -hex 16`); AutoKuma silently refuses the
   monitor otherwise (`Invalid push_token`).
2. `n8n_api_key`: minted in n8n → Settings → n8n API, scoped to read Workflow + Execution.
3. `sonarr_api_key` / `radarr_api_key` / `bazarr_api_key` / `prowlarr_api_key`: the apps'
   own keys; re-derive from `/config/config.xml` inside the pod if needed.
4. `cloudflare_analytics_token`: a Custom token with exactly **Account → Account Analytics →
   Read** — never write or R2 permissions, which would let the bridge hard-stop the bucket it
   guards; the monitor pages and a human decides. `secret_rotation.py sync`, then smoke-test
   with `--once`. The bucket's 7-day `AbortIncompleteMultipartUpload` lifecycle rule is set by
   hand (`wrangler r2 bucket lifecycle add`); the uploads arm notices it missing.
5. `monitor_bridge_ha_token`: an HA Long-Lived Access Token (Profile → Security), tier
   `assisted`.
6. `healthchecks_api_read_only_key`: the Healthchecks.io project's **read-only** API key
   (project Settings → API Access), tier `assisted`. The `healthchecks_drift` check only
   reads the console, so it must never hold the full key, which can edit and delete checks.
7. Notifications attach automatically through the `kuma()` macro's `notification_name_list`.

## Module layout — and the one rule that governs it

`files/` holds four flat modules and three packages: `bridge/` (the plumbing every check
shares), `checks/` (one module per domain of `check_*` bodies, mirroring the test file for
that domain) and `verdicts/` (pure logic taking its inputs as arguments). `registry.py` and
`gates.py` import `bridge.types` and the `checks.*` bodies and never each other or `check`. A
module imports a sibling by package (`from bridge.config import Config`): `/app` is
`sys.path[0]` in the pod and `files/` is on `pythonpath` under pytest. No `__init__.py`.
Modules split at a 600-line cap.

**Adding a module means adding its path to `monitor_bridge_modules`** in `defaults/main.yml`
(`checks/newdomain.py`, shipped as the flat ConfigMap key `checks_newdomain.py` and mounted
back at its path by the Deployment's `items:`). A module missing from it kills the pod at
import on its next roll, on the one workload that cannot page about its own failure, and
pytest cannot see it because it imports from `files/`.
`ansible/tests/services/test_monitor_bridge_modules.py` and
`test_monitor_bridge_mount_layout.py` beside it are what do.

| module | holds |
|---|---|
| `cli.py` | the `argparse` front end and `main(argv, env, checks, gate_config) -> int`, which builds the `Config`, the registry and the `Gates`, validates the check filter and loops `run_once` |
| `check.py` | `run_once(cfg, checks, gates, dry_run, only)` — the run loop, and nothing else |
| `registry.py` | `build_checks(env)`, every `Check` with its `KUMA_PUSH_*` name read from the environment it is handed |
| `gates.py` | the five `*_DEPENDENT` sets, `STARTUP_GRACE`, `GATE_DEPENDENTS`, `check_enabled`, `validate_check_filter`, `expand_gates_for_cli`, `down_exporters`, `_evaluate`, `_gate`, and the frozen `Gates` seam `run_once` reads every gate fact through |
| `bridge/types.py` | `Check`, `CheckResult`, `CheckFn` — shared by `registry.py` and `check.py` without either importing the other |
| `checks/<domain>.py` | the `check_*` bodies by domain: `service`, `gitops`, `notify`, `logs`, `cluster` (+ `cluster_etcd`, `cluster_rollout`, `cluster_traefik`, `cluster_zero`), `host`, `host_thermal`, `host_edge`, `b2`, `r2`, `cloudflare_ips`, `healthchecks`, `storage`. `checks/gitops.py` holds `gitops_status` beside its check — the one verdict that reads `cfg` itself — and its parsers come from `gitops_markers.py`, the generated copy of the deployer's module. `host_edge`'s entry points take the prober as `tcp_open` so a test injects a port map |
| `bridge/config.py` + `config_{host,service,cluster,io}.py` | the `_env`/`_int`/`_num`/`_env_file` parsers, `class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig)`, `load_config(env)`; one builder per domain. `K8S_EXTENDED_RESOURCES` and `PVC_EXCLUDE` stay in `config.py` because a repo test greps for them by text |
| `bridge/net.py` | `_get_json`, `_post_json`, `prom_scalar`, `prom_vector`, the `loki_*` queries, `push` with `cap_push_msg` (`PUSH_MSG_MAX`), and the selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`). Every helper that reads a URL or the origin pin takes `cfg` FIRST |
| `bridge/msgfmt.py` | `format_down(unit, state, items, details)` — the one grammar for a push message naming several things; import-free so `probe.py releases --kuma` loads it from a host too |
| `bridge/streaks.py` | `down_streak` (the consecutive-down counter four domains share, cleared by `conftest.py`) and `apply_startup_grace` |
| `bridge/common.py` | `_env`, `sanitize` — the two helpers shared verbatim with autofix-bridge's `autofix.py`; its header records what was considered and rejected — plus `host_uptime_s`, the node's boot clock the two post-reboot arms key on |
| `bridge/parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts/<domain>.py` | pure decisions taking every threshold as an argument: `cluster`, `host` (`hwmon_temp_limits`, `pi_pressure`), `host_smart` (`scrutiny_*`), `host_power` (`ups_health`, `thermal_monitor_verdict`), `host_cgroups`, `service`, `logs`, `notify`, `storage` (the B2/R2 decisions with the R2 Class A/B/free ACTION LISTS beside them — policy, not configuration) |

## Configuration is a parameter, not a module global

`cli.py`'s `main()` calls `load_config(os.environ)` ONCE and hands the frozen `Config` to
`check.run_once`, which passes it to every gate, every check body (`Check.fn(cfg)`) and every
`bridge.net` helper that reads a URL; the registry (`build_checks(env)`) and the `Gates` are
built there too and passed in, so a test states them rather than patching tables. Building the
config MUST NOT raise: `_int`/`_num` record a malformed value in
`bridge.common.CONFIG_PROBLEMS`, keep the default, and `main()` reports each and exits 2.

- **A default argument cannot read the config** — defaults evaluate at import. Default to
  `None` and resolve `cfg.X` in the body.
- **A test states its configuration**: the `cfg` fixture is `load_config({})`; narrow it with
  `dataclasses.replace(cfg, X=...)`, or call `load_config({...})` when the READ is under test.
- **A `verdicts/` module reads no `cfg`** and takes every threshold as an argument.
- **A test patches the module that READS the name, and a module reads it qualified**
  (`bridge.net._get_json` at call time, never `from bridge.net import _get_json`). Getting it
  wrong is silent, so `ansible/tests/services/test_monitor_bridge_modules.py` re-derives every
  patched `(module, name)` pair by AST and `test_bridge_patch_boundary.py` beside it fails a
  runtime module that from-imports a patched name.
- Mutable per-check state stays with the code that mutates it; `_down_streaks` has its own
  module only because four domains mutate it.

## Editing & testing
- Manifests: `templates/deployment.yaml.j2`, `templates/env-secret.yaml.j2` · Logic:
  `files/cli.py` plus the modules beside it (*Module layout* above)
- Unit tests: `uv run pytest ansible/roles/k8s/monitor-bridge/tests`, one file per domain
  (`test_check_<domain>.py`); put a new test with the domain it exercises.
- **A shared test helper goes under `tests/`, never beside `cli.py`** — every `.py` in
  `files/` is production code to `_runtime_modules()`. A fixture goes in `tests/conftest.py`;
  a helper taking arguments goes in an underscore-prefixed, repo-unique module
  (`_check_gate_helpers.py`), never `from conftest import ...`.
- Smoke test one pass:
  `sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once`
  (the readonly SA holds no exec verb). `--dry-run` evaluates every check and pushes nothing,
  so a hand run cannot overwrite a live monitor's state; `--check <name>` narrows it, validated
  like `CHECKS_ONLY`, including the refusal to run a gated check without its gate.
- Deploy: `./scripts/deploy.sh --tags "monitor-bridge"`

## Traps

Each rule here came from an incident; `docs/monitor-bridge-checks.md`'s *Traps* section holds
the evidence for each.

- **kube-state-metrics sanitizes resource names into labels:** `devic.es/dri` is emitted as
  `resource="devic_es_dri"`. Sanitize at query time (`ksm_resource_label`), keep the
  operator-facing name the one `kubectl` prints, and name both forms in the message — a
  fail-closed check cannot tell "deregistered" from "wrong question".
- **Promtail's k8s streams have no `app` label** — they carry `container` / `pod` / `job` /
  `namespace` / `service_name`. A selector written from the `-l app=` habit matches nothing,
  and a fail-open arm reads that as a clean bill of health. `LOKI_STREAM_LABELS` and
  `test_loki_selectors_use_real_stream_labels` pin the vocabulary.
- **The runtime stamps the log lines; `bridge.common.log` does not.** Pass `--timestamps` to
  `kubectl logs` and read the prefix as UTC. A line archived before 2026-08-16 carries a
  bracketed America/Chicago stamp, so cross-check one line against `date -u` before anchoring
  a timeline on it.
