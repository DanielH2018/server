# monitor-bridge internals — module table, prerequisites and the test seams

Working-out moved off `ansible/roles/k8s/monitor-bridge/CLAUDE.md` (#2993), which a session reads on
every touch of the alert pipeline. The role doc keeps the rules; this page keeps the per-module
table, the gate-set membership, the operator prerequisites and the test-side seams.
`docs/monitor-bridge-checks.md` is the sibling page: the per-check record that links here for
the plumbing.

## Gate-set membership

Every set but `EXPORTER_DEPENDENT` derives from the `gate` column of
`ansible/roles/k8s/monitor-bridge/files/check_table.py`, and each member's row carries the
reason it is a member. The table lists the members, generated from that column.

--8<-- "assets/generated/fragments/bridge-gate-sets.md"

The rules below shaped those memberships.

- **`LOKI_DEPENDENT`**: `ha_heartbeat` is deliberately NOT a member. Its ban arm fails open on a
  Loki error, so the heartbeat verdict survives a Loki outage.
- **`B2_DEPENDENT`**: the gate runs one `b2_authorize_account` per `B2_PROBE_INTERVAL_S`
  (1800 s) with a BILLED outcome cached, because the fault it detects is a transaction cap. A
  failure that never reached B2 takes the short `B2_TRANSPORT_RETRY_S` TTL.
- **`WAN_DEPENDENT`**: the gate reads two provider URLs by hostname and is DOWN only when
  NEITHER answers, so one provider's outage cannot silence a dependent reading the other. It
  never probes a shared-anycast IP, because the 2026-09-18 outage included DNS failure.
- **`EXPORTER_DEPENDENT`** — `node` → disk, memory, `host_temp`; `node-pi` → `host_temp`,
  `pi_pressure`. `pvc_fullness` gets no entry keyed on the kubelet job on purpose: its claim-count
  floor exists to page on exactly that partial outage.
- **`STARTUP_GRACE`** is disjoint from every skip set, so a check that gains a gate leaves
  this set; a test holds both.
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
- **Push tokens and tiles:** `files/check_table.py` holds one `PushCheck` row per check and
  gate (#3659, #3788). The env-secret renders `KUMA_PUSH_<NAME>` for each row, the name
  `bridge.types.push_env` reads, from the SOPS secret the row's `token` names. uptime-kuma's
  `static-monitors.yaml.j2` renders each row's push tile from its `kuma_id`, `display`,
  `description`, `critical` and `runbook` (#3781). Both templates read the table through
  `ansible/filter_plugins/py_table.py`, which parses the module instead of importing it.
  `tests/test_check_table.py` pins the table to the registry, the gates, the env, SOPS and
  that parse, and `test_every_bridge_push_token_reaches_a_push_tile` pins it to the AutoKuma
  monitors.
- **A table change deploys uptime-kuma too.** `deploy_cross_role.k8s_lookup_readers` finds
  every k8s role that `lookup()`s another role's file, and the landing, the deployer's defer
  alert and the secret census all read it.
- **Fold an arm into an existing monitor** when it answers that tile's existing question
  (cgroups into Memory, ports into Pi Pressure, `ip_ban` into HA); a new tile costs a push token in
  SOPS and a monitor created by hand.
- **File-mounted credentials** (`HA_TOKEN_FILE`, `B2_PROBE_APPLICATION_KEY_FILE` reusing
  `longhorn_b2_application_key`, `CF_ANALYTICS_TOKEN_FILE`, `SPEEDTEST_TOKEN_FILE`,
  `HEALTHCHECKS_API_KEY_FILE`) are rendered 0600 and read through `bridge.config._env_file`; an
  empty file disables the check. Ids stay inline.
- Thresholds are env-tunable in `templates/env-secret.yaml.j2`, the only place each value is
  written: `bridge/config*.py` reads a rendered key with no default (#3659). A failed query makes that monitor `down` with an explanatory message.

## Operator prerequisites

1. A push token in `secrets.yml` for every `check_table.py` row — exactly 32 alphanumeric characters
   (`openssl rand -hex 16`); AutoKuma silently refuses the monitor otherwise
   (`Invalid push_token`).
2. `n8n_api_key`: minted in n8n → Settings → n8n API, scoped to read Workflow + Execution.
3. `sonarr_api_key` / `radarr_api_key` / `bazarr_api_key` / `prowlarr_api_key`: the apps' own
   keys; re-derive from `/config/config.xml` inside the pod if needed.
4. `cloudflare_analytics_token`: a Custom token with exactly **Account → Account Analytics →
   Read** — never write or R2 permissions, which would let the bridge hard-stop the bucket it
   guards; the monitor pages and a human decides. Run `secret_rotation.py sync`, then smoke-test
   with `--once`: the unit tests mock the payload, so only the live run proves Cloudflare
   accepts the query and the token's scope. The check also reads the existing `r2_account_id`
   and `r2_bucket`.
   - **Break-glass stop:** a hard stop is a manual step, never routine. Revoke the key under
     R2 → Manage R2 API Tokens, and restore the path by re-minting it and updating
     `r2_access_key_id` and `r2_secret_access_key`. A token that could revoke R2 access would
     park a more privileged standing credential in the cluster, and its firing would break the
     backup path it guards.
   - **Lifecycle rule:** the bucket's 7-day `AbortIncompleteMultipartUpload` rule is set by hand,
     because it needs the S3 API or Wrangler and the stdlib-only bridge has neither:
     `npx wrangler r2 bucket lifecycle add <bucket> --name abort-mpu --abort-multipart-days 7`,
     or dashboard → R2 → the bucket → Settings → Object Lifecycle Rules. The uploads arm
     notices it missing.
5. `monitor_bridge_ha_token`: an HA Long-Lived Access Token (Profile → Security), tier
   `assisted`.
6. `healthchecks_api_read_only_key`: the Healthchecks.io project's **read-only** API key (project
   Settings → API Access), tier `assisted`. The `healthchecks_drift` check only reads the console,
   so it must never hold the full key, which can edit and delete checks.
7. Notifications attach automatically through the `kuma()` macro's `notification_name_list`.

## The module table

| module | holds |
|---|---|
| `cli.py` | the `argparse` front end and `main(argv, env, checks, gate_config, sources) -> int`, which builds the `Config`, the registry, the `Gates` and the `Sources`, validates the check filter and loops `run_once` |
| `check.py` | `run_once(cfg, src, checks, gates, sink, dry_run, only)` — the run loop, and nothing else |
| `check_table.py` | `CHECKS`, one `PushCheck` per check and gate: its body, push token, gate and Kuma tile |
| `registry.py` | `build_checks(env)`, a `Check` for every non-gate row of `CHECKS`, with its token read from the environment it is handed |
| `gates.py` | the `*_DEPENDENT` sets derived from `CHECKS`' `gate` column, `STARTUP_GRACE`, `GATE_DEPENDENTS`, `check_enabled`, `validate_check_filter`, `expand_gates_for_cli`, `down_exporters`, `_evaluate`, `_gate`, and the frozen `Gates` seam `run_once` reads every gate fact through |
| `gitops_markers.py`, `gitops_ledger.py`, `gitops_hold.py` | generated verbatim copies of the deployer's marker parsers, written by `scripts/dev/gen_gitops_markers.py` (edit the source under `roles/setup/gitops_deploy/files/`, never the copy). `gitops_markers.py` holds the line-format markers, `gitops_ledger.py` the `owed` ledger and tick receipt, and `gitops_hold.py` the `DeployerSnapshot` that `check_gitops_status` reads every marker through |
| `longhorn_robustness.py` | `SAFE_ROBUSTNESS` and `unsafe_volumes` — which Longhorn robustness states are lost redundancy. Import-free, because `scripts/deploy_tools/runbook_gates_lib/gate_runner.py` imports the same file from the checkout to judge the CRs (#3668) |
| `bridge/types.py` | `PushCheck`, `Check`, `CheckResult`, `CheckFn` — shared by `registry.py` and `check.py` without either importing the other |
| `checks/<domain>.py` | the `check_*` bodies by domain: `service`, `gitops`, `notify`, `logs`, `cluster` (+ `cluster_etcd`, `cluster_rollout`, `cluster_traefik`, `cluster_zero`), `host`, `host_thermal`, `host_edge`, `b2`, `r2`, `cloudflare_ips`, `healthchecks`, `storage`, `wan`. `checks/gitops.py` holds `gitops_status` beside its check — the one verdict that reads `cfg` itself — and its parsers come from `gitops_markers.py` and `gitops_ledger.py`, the generated copies of the deployer's modules; the second reads the `owed` ledger's `manual_plane`, `k8s_deferred` and `hold_plane` classes. `check_gitops_status` reads every marker it judges through `gitops_hold.DeployerSnapshot`, the third generated copy. `host_edge`'s entry points take the probe function as `tcp_open` so a test injects a port map |
| `bridge/config.py` + `config_{host,service,cluster,io}.py` | the `_env`/`_int`/`_num`/`_env_file` parsers, `class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig)`, `load_config(env)`; one builder per domain. `K8S_EXTENDED_RESOURCES` and `PVC_EXCLUDE` stay in `config.py` because a repo test greps for them by text |
| `bridge/net.py` | the transport: `_get_json`, `_post_json`, `prom_scalar`, `prom_vector`, the `loki_*` queries, `push` (which caps its message with `bridge.common.cap_push_msg`), and the selector builders (`origin_sel`, `cadvisor_sel`, `host_metric_sel`). Every helper that reads a URL or the origin pin takes `cfg` FIRST. No check body calls its fetchers; they go through `bridge/sources.py` |
| `bridge/sources.py` | `Sources(cfg)`, every query a gate or check body sends (`prom_scalar`, `prom_vector`, `loki_count`, `loki_vector`, `loki_lines`, `get_json`, `post_json`, and `log_error_counts` composed from two of them). `cli.main()` builds one; `run_once` hands it to every body as `src`. Also `Sink(cfg)`, where `run_once` pushes every verdict, delegating to `bridge.net.push` |
| `bridge/msgfmt.py` | `format_down(unit, state, items, details)` — the one grammar for a push message naming several things; import-free so `probe.py releases --kuma` loads it from a host too |
| `bridge/streaks.py` | `State` (every streak counter and probe cache, carried as `src.state`), `down_streak` (the consecutive-down step) and `apply_startup_grace` |
| `bridge/common.py` | `_env`, `sanitize`, `cap_push_msg` (`PUSH_MSG_MAX`) and `clamp_discord` (`DISCORD_MAX`) — the helpers shared verbatim with autofix-bridge's `autofix.py`; its header records what was considered and rejected — plus `host_uptime_s`, the node's boot clock the two post-reboot arms key on |
| `bridge/parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts/<domain>.py` | pure decisions taking every threshold as an argument: `cluster`, `host` (`hwmon_temp_limits`, `pi_pressure`), `host_smart` (`scrutiny_*`), `host_power` (`ups_health`, `thermal_monitor_verdict`), `host_cgroups`, `service`, `logs`, `notify`, `storage` (the B2/R2 decisions with the R2 Class A/B/free ACTION LISTS beside them — policy, not configuration) |

## The test seams

- **A test states its configuration.** The `cfg` fixture is `load_config(bridge_env())`, the
  rendered `templates/env-secret.yaml.j2`, because a key the template renders has no Python
  default (#3659); narrow it with
  `dataclasses.replace(cfg, X=...)`, or call `load_config(bridge_env(X=...))` when the READ is
  under test.
- **A test states what a check reads.** Every check body and gate probe takes `(cfg, src)`, and
  a test hands it `tests/_fake_sources.py`'s `FakeSources(prom_vector=lambda q: ...)` rather
  than patching `bridge.net` (#3742). An unanswered query raises, so `FakeSources()` proves a
  check does no I/O; `src.queries("prom_vector")` lists the PromQL it sent.
- **A test states where results go and how a request is sent.** `run_once` pushes through the
  `sink` argument, a `bridge.sources.Sink`, and a test hands it `FakeSink()` and reads
  `sink.pushes`. Every `bridge.net` request function, and `Sources` and `Sink`, take an `opener`
  in `urllib.request.urlopen`'s shape, and the SMTP backstop takes its `login`, so no test
  patches `bridge.net.push`, `urlopen` or `_smtp_login_ok` (#3938).
- **The loop's clock is a parameter as well.** `cli.main` takes `sleep`, and a test points
  `HEARTBEAT_FILE` at the null device through the env it hands `main`, so no test patches
  `time.sleep`, `bridge.common.touch_heartbeat` or `bridge.common.log` (#3986). No monitor-bridge
  test patches a runtime module, and `ansible/tests/repo/monkeypatch_allowlist.txt` has no row
  for this role.
- **A test that does patch one must patch the module that READS the name, and a module reads
  it qualified** (`bridge.net.push` at call time, never `from bridge.net import push`). Getting it
  wrong is silent, so `ansible/tests/services/test_monitor_bridge_modules.py` re-derives every
  patched `(module, name)` pair by AST, and `test_bridge_patch_boundary.py` beside it fails a
  runtime module that from-imports a patched name.
- **Per-check state is a parameter too.** Every streak counter and probe cache is a field of
  `bridge.streaks.State`, which `Sources` builds once, so a check reads `src.state.down_streaks`
  and a fresh `FakeSources` starts zeroed (#3866). The startup grace's counters are
  `src.state.grace_streaks` (#3938). A test whose cycles each build their own fake
  hands them one `State`: `FakeSources(state=...)`, or conftest's `state` fixture. Without
  that, a "resets the streak" test passes on a counter that never advanced.
- A fixture goes in `tests/conftest.py`; a helper taking arguments goes in an
  underscore-prefixed, repo-unique module (`_check_gate_helpers.py`), never
  `from conftest import ...`.
