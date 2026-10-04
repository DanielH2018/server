# monitor-bridge internals — module table, prerequisites and the test seams

Working-out moved off `ansible/roles/k8s/monitor-bridge/CLAUDE.md` (#2993), which a session reads on
every touch of the alert pipeline. The role doc keeps the rules; this page keeps the per-module
table, the gate-set membership, the operator prerequisites and the test-side seams.
`docs/monitor-bridge-checks.md` is the sibling page for the checks themselves.

## Gate-set membership

- **`LOKI_DEPENDENT`** — `loki_ingestion`, `swallowed_verdicts`, `kuma_notify_failures`.
  `ha_heartbeat` is deliberately NOT a member: its ban arm fails open on a Loki error, so the
  heartbeat verdict survives a Loki outage.
- **`B2_DEPENDENT`** — `b2_storage`. The gate runs one `b2_authorize_account` per
  `B2_PROBE_INTERVAL_S` (1800 s) with a BILLED outcome cached, because the fault it detects is a
  transaction cap; a failure that never reached B2 takes the short `B2_TRANSPORT_RETRY_S` TTL.
- **`WAN_DEPENDENT`** — `r2_usage`, `cloudflare_ips_drift`, `healthchecks_drift`, `discord`. The
  gate reads two provider URLs by hostname and is DOWN only when NEITHER answers, so one
  provider's outage cannot silence a dependent reading the other. Never a shared-anycast IP: the
  2026-09-18 outage included DNS failure.
- **`EXPORTER_DEPENDENT`** — `node` → disk, memory, `host_temp`; `node-pi` → `host_temp`,
  `pi_pressure`. `pvc_fullness` gets no entry keyed on the kubelet job on purpose: its claim-count
  floor exists to page on exactly that partial outage.
- **`STARTUP_GRACE`** — `n8n`, `bazarr`, `prowlarr_indexers`, `scrutiny`, `speedtest`. Disjoint
  from every skip set, so a check that gains a gate leaves this set; a test holds both.
- **The retired second Prometheus gate.** `Cluster Prometheus Reachable` gated a
  `CLUSTER_DEPENDENT` set reading `CLUSTER_PROMETHEUS_URL` until 2026-09-28. Both URLs named one
  cluster Service after the Docker plane retired (2026-08-14), so its tile could not go red on its
  own and read as coverage that did not exist; #2825 folded its four members into
  `PROM_DEPENDENT` and deleted the gate, the URL, the push token and the tile. Reintroducing a
  second Prometheus is a `git revert`.

## Push-monitor plumbing

- **The heartbeat window is `uptime_kuma_k8s_bridge_push_interval` = 1200 s**, 4 × the loop.
- **Liveness probe:** `cli.py` touches `/tmp/heartbeat` after every cycle and the probe in
  `templates/deployment.yaml.j2` fails past ~3×INTERVAL, so the kubelet restarts a hung loop.
- **Push tokens:** `templates/env-secret.yaml.j2`'s `KUMA_PUSH_*` keys are the list, one SOPS
  `monitor_bridge_<check>_push_token` each. `test_every_push_token_env_is_wired_to_a_monitor`
  pins them to the AutoKuma monitors and `test_checks_and_env_secret_push_tokens_agree` to the
  registry.
- **Fold an arm into an existing monitor** when it answers that tile's existing question
  (cgroups into Memory, ports into Pi Pressure, `ip_ban` into HA); a new tile costs a push token in
  SOPS and a monitor created by hand.
- **File-mounted credentials** (`HA_TOKEN_FILE`, `B2_PROBE_APPLICATION_KEY_FILE` reusing
  `longhorn_b2_application_key`, `CF_ANALYTICS_TOKEN_FILE`, `SPEEDTEST_TOKEN_FILE`,
  `HEALTHCHECKS_API_KEY_FILE`) are rendered 0600 and read through `bridge.config._env_file`; an
  empty file disables the check. Ids stay inline.
- Thresholds are env-tunable in `templates/env-secret.yaml.j2`, and `bridge/config*.py` names the
  default for each. A failed query makes that monitor `down` with an explanatory message.

## Operator prerequisites

1. A push token in `secrets.yml` for every `KUMA_PUSH_*` key — exactly 32 alphanumeric characters
   (`openssl rand -hex 16`); AutoKuma silently refuses the monitor otherwise
   (`Invalid push_token`).
2. `n8n_api_key`: minted in n8n → Settings → n8n API, scoped to read Workflow + Execution.
3. `sonarr_api_key` / `radarr_api_key` / `bazarr_api_key` / `prowlarr_api_key`: the apps' own
   keys; re-derive from `/config/config.xml` inside the pod if needed.
4. `cloudflare_analytics_token`: a Custom token with exactly **Account → Account Analytics →
   Read** — never write or R2 permissions, which would let the bridge hard-stop the bucket it
   guards; the monitor pages and a human decides. Run `secret_rotation.py sync`, then smoke-test
   with `--once`. The bucket's 7-day `AbortIncompleteMultipartUpload` lifecycle rule is set by
   hand (`wrangler r2 bucket lifecycle add`); the uploads arm notices it missing.
5. `monitor_bridge_ha_token`: an HA Long-Lived Access Token (Profile → Security), tier
   `assisted`.
6. `healthchecks_api_read_only_key`: the Healthchecks.io project's **read-only** API key (project
   Settings → API Access), tier `assisted`. The `healthchecks_drift` check only reads the console,
   so it must never hold the full key, which can edit and delete checks.
7. Notifications attach automatically through the `kuma()` macro's `notification_name_list`.

## The module table

| module | holds |
|---|---|
| `cli.py` | the `argparse` front end and `main(argv, env, checks, gate_config) -> int`, which builds the `Config`, the registry and the `Gates`, validates the check filter and loops `run_once` |
| `check.py` | `run_once(cfg, checks, gates, dry_run, only)` — the run loop, and nothing else |
| `registry.py` | `build_checks(env)`, every `Check` with its `KUMA_PUSH_*` name read from the environment it is handed |
| `gates.py` | the five `*_DEPENDENT` sets, `STARTUP_GRACE`, `GATE_DEPENDENTS`, `check_enabled`, `validate_check_filter`, `expand_gates_for_cli`, `down_exporters`, `_evaluate`, `_gate`, and the frozen `Gates` seam `run_once` reads every gate fact through |
| `bridge/types.py` | `Check`, `CheckResult`, `CheckFn` — shared by `registry.py` and `check.py` without either importing the other |
| `checks/<domain>.py` | the `check_*` bodies by domain: `service`, `gitops`, `notify`, `logs`, `cluster` (+ `cluster_etcd`, `cluster_rollout`, `cluster_traefik`, `cluster_zero`), `host`, `host_thermal`, `host_edge`, `b2`, `r2`, `cloudflare_ips`, `healthchecks`, `storage`. `checks/gitops.py` holds `gitops_status` beside its check — the one verdict that reads `cfg` itself — and its parsers come from `gitops_markers.py` and `gitops_ledger.py`, the generated copies of the deployer's modules; the second reads the `owed` ledger's `manual_plane`, `k8s_deferred` and `hold_plane` classes. `host_edge`'s entry points take the probe function as `tcp_open` so a test injects a port map |
| `bridge/config.py` + `config_{host,service,cluster,io}.py` | the `_env`/`_int`/`_num`/`_env_file` parsers, `class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig)`, `load_config(env)`; one builder per domain. `K8S_EXTENDED_RESOURCES` and `PVC_EXCLUDE` stay in `config.py` because a repo test greps for them by text |
| `bridge/net.py` | `_get_json`, `_post_json`, `prom_scalar`, `prom_vector`, the `loki_*` queries, `push` (which caps its message with `bridge.common.cap_push_msg`), and the selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`). Every helper that reads a URL or the origin pin takes `cfg` FIRST |
| `bridge/msgfmt.py` | `format_down(unit, state, items, details)` — the one grammar for a push message naming several things; import-free so `probe.py releases --kuma` loads it from a host too |
| `bridge/streaks.py` | `down_streak` (the consecutive-down counter four domains share, cleared by `conftest.py`) and `apply_startup_grace` |
| `bridge/common.py` | `_env`, `sanitize`, `cap_push_msg` (`PUSH_MSG_MAX`) and `clamp_discord` (`DISCORD_MAX`) — the helpers shared verbatim with autofix-bridge's `autofix.py`; its header records what was considered and rejected — plus `host_uptime_s`, the node's boot clock the two post-reboot arms key on |
| `bridge/parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts/<domain>.py` | pure decisions taking every threshold as an argument: `cluster`, `host` (`hwmon_temp_limits`, `pi_pressure`), `host_smart` (`scrutiny_*`), `host_power` (`ups_health`, `thermal_monitor_verdict`), `host_cgroups`, `service`, `logs`, `notify`, `storage` (the B2/R2 decisions with the R2 Class A/B/free ACTION LISTS beside them — policy, not configuration) |

## The test seams

- **A test states its configuration.** The `cfg` fixture is `load_config({})`; narrow it with
  `dataclasses.replace(cfg, X=...)`, or call `load_config({...})` when the READ is under test.
- **A test patches the module that READS the name, and a module reads it qualified**
  (`bridge.net._get_json` at call time, never `from bridge.net import _get_json`). Getting it
  wrong is silent, so `ansible/tests/services/test_monitor_bridge_modules.py` re-derives every
  patched `(module, name)` pair by AST, and `test_bridge_patch_boundary.py` beside it fails a
  runtime module that from-imports a patched name.
- **Mutable per-check state stays with the code that mutates it.** `_down_streaks` has its own
  module only because four domains mutate it.
- A fixture goes in `tests/conftest.py`; a helper taking arguments goes in an
  underscore-prefixed, repo-unique module (`_check_gate_helpers.py`), never
  `from conftest import ...`.
