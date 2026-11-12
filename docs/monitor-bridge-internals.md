# monitor-bridge internals — module table, prerequisites and the test seams

Working-out moved off `ansible/roles/k8s/monitor-bridge/CLAUDE.md` (#2993), which a session reads on
every touch of the alert pipeline. The role doc keeps the rules; this page keeps the per-module
table, the gate-set membership, the operator prerequisites and the test-side seams.
`docs/monitor-bridge-checks.md` is the sibling page: the per-check record that links here for
the plumbing.

## Gate membership

Every gate's members derive from the `gate` column of
`ansible/roles/k8s/monitor-bridge/files/check_table.py`, and each member's row carries the
reason it is a member. `check.EXPORTER_DEPENDENT` is the one map the column does not hold, because it is keyed by scrape job. The table lists the members, generated from that column.

--8<-- "assets/generated/fragments/bridge-gate-sets.md"

The rules below shaped those memberships.

- **There is no Loki gate.** The `loki_reachable` gate left with M5's PR 4a, and a Loki fault
  pages from the `loki-homelab` ruler's `LokiHomelabMetricsMissing`. `ha_heartbeat` reads Loki
  and fails open on a Loki error, so it keeps its own verdict and sits in no gate set.
- **`b2_reachable`**: the gate runs one `b2_authorize_account` per `B2_PROBE_INTERVAL_S`
  (1800 s) with a BILLED outcome cached, because the fault it detects is a transaction cap. A
  failure that never reached B2 takes the short `B2_TRANSPORT_RETRY_S` TTL.
- **`wan_reachable`**: the gate reads two provider URLs by hostname and is DOWN only when
  NEITHER answers, so one provider's outage cannot silence a dependent reading the other. It
  never probes a shared-anycast IP, because the 2026-09-18 outage included DNS failure.
- **`check.EXPORTER_DEPENDENT`** — `node` → `host_packages`. The disk, memory, host temperature and
  UPS checks left in M6 batch A1: the `hosts` rules start every missing-data arm from
  `up == 1`, so a dead exporter leaves them quiet beside `ScrapeTargetDown`. `pi_pressure` and
  its `node-pi` entry left in batch B8, and `PiHostDown` inhibits `PiPortDead` in Alertmanager.
- **`startup_grace`** is one value of the same column as the gate names. The bridge holds
  nothing for it since M6 batch A6's Cutover-2: the row's rule in `bridge-exporter.yaml` carries
  `for: 10m`, which rides out the weekly reboot. The value stays so the rules and the table
  agree on which rows have that grace.
- **The retired second Prometheus gate.** `Cluster Prometheus Reachable` gated a
  `CLUSTER_DEPENDENT` set reading `CLUSTER_PROMETHEUS_URL` until 2026-09-28. Both URLs named one
  cluster Service after the Docker plane retired (2026-08-14), so its tile could not go red on its
  own and read as coverage that did not exist; #2825 folded its four members into
  the `prometheus` gate's members and deleted the gate, the URL, the push token and the tile. Reintroducing a
  second Prometheus is a `git revert`. The first Prometheus gate row went with the push in A6's
  Cutover-2: `host_packages` keeps `gate="prometheus"` as the label of the `EXPORTER_DEPENDENT`
  suppression, with no gate row behind it.

## Exporter plumbing

- **HISTORY — the bridge's Kuma push.** M6 batch A6's Cutover-2 deleted the push path,
  and the `KUMA_PUSH_*` env keys. `uptime_kuma_k8s_bridge_push_interval` went with the uptime-kuma
  role. A6-9 retired the 20
  `monitor_bridge_*_push_token` keys and deleted the pending-retirement allowance.
- **Liveness probe:** `cli.py` touches `/tmp/heartbeat` after every cycle and the probe in
  `templates/deployment.yaml.j2` fails past ~3×INTERVAL, so the kubelet restarts a hung loop.
- **The table:** `files/check_table.py` holds one `PushCheck` row per check and gate (#3659,
  #3788). `registry.build_checks` reads it in Python. `ansible/filter_plugins/py_table.py`
  parses it without importing it for the docs fragments. `tests/test_check_table.py` pins the
  table to the registry, the gates, the env and that parse. A row carries `name`, `fn`, `gate`,
  `is_gate`, `critical` and `runbook`: A6-8 deleted `token`, `kuma_id`, `display`, `description`
  and `status_group` with the tiles they fed. `tests/test_bridge_size_cap.py` holds the bridge's
  `files/` under 8,000 lines.
- **A table change deploys monitor-bridge alone.** `scripts/lib/k8s_lookups.py:k8s_lookup_map` finds
  every k8s role that `lookup()`s another role's file, and uptime-kuma stopped being one for the
  table when its bridge tiles went.
- **Fold an arm into an existing check** when it answers that check's existing question
  (`ip_ban` into HA); a new check costs a row, a body and a rule in `bridge-exporter.yaml`.
- **File-mounted credentials** (`HA_TOKEN_FILE`, `B2_PROBE_APPLICATION_KEY_FILE` reusing
  `longhorn_b2_application_key`, `CF_ANALYTICS_TOKEN_FILE`, `SPEEDTEST_TOKEN_FILE`,
  `HEALTHCHECKS_API_KEY_FILE`) are rendered 0600 and read through `bridge.config._env_file`; an
  empty file disables the check. Ids stay inline.
- Thresholds are env-tunable in `templates/config/env.yaml.j2`, the only place each value is
  written: `bridge/config*.py` reads a rendered key with no default (#3659). A failed query makes that check `down` with an explanatory message.

## The two exporters

The role `CLAUDE.md` keeps the rules; this is the wiring.

- **The check exporter.** `files/verdict_export.py` serves each row's verdict
  (`docs/target-architecture/monitoring/m6-retire-kuma.md`, *The bridge as an exporter*). It
  runs on the `metrics` item of the role's `containers_list` entry, and observability's
  `monitor-bridge` scrape job collects it. `homelab_check_success`,
  `homelab_check_last_run_timestamp_seconds` and `homelab_check_deadline_seconds` carry
  `check="<row>"`. `homelab_check_rows` counts the enabled rows of `CHECKS`, gate rows
  included, so it is not `registry.build_checks`' length, which drops every gate row.
  `BridgeCheckStale` compares the exported row count against it.
- **The deploy-pipeline family.** `files/deploy_pipeline.py` reads `promote`'s state after
  every check has run and serves its gauges on `monitor_bridge_deploy_pipeline_port` through
  `templates/service.yaml.j2`. observability's `deploy-pipeline` scrape job collects them and
  its `deploy-pipeline` rule group pages on them; `docs/flux-deploy-plane.md` says what each
  alert means. The token comes from the SOPS key `monitor_bridge_deploy_pipeline_github_token`.
  An empty token serves no series, which `DeployPipelineExporterAbsent` pages. Each cycle logs
  one `deploy-pipeline:` line with the values and any failed read.
- **The reboot-window gauge reads no remote state.** It derives from the `WEEKLY_REBOOT_*` env
  keys, which `templates/config/env.yaml.j2` renders from the `weekly_reboot_*` group vars, so
  PromotionStalled stays quiet while `promote` defers across the weekly reboot (#5458).
- **The deploy-pipeline scrape port is a literal in `prometheus.yaml.j2`.**
  `ansible/tests/services/test_deploy_pipeline_scrape.py` holds it to the role's default.
- **The gates' labels.** B2 and WAN Reachable skip the rows naming them. The `prometheus` gate
  value suppresses no row in the bridge: it labels the rows `EXPORTER_DEPENDENT` holds and the
  rules that carry the exporter inhibit. `registry.build_checks` hands each row's `gate` to
  `check.run_once`.

## Module mounting

- **One ConfigMap per module.** `templates/script-configmap.yaml.j2` renders each
  `monitor_bridge_modules` entry as its own ConfigMap, and the Deployment's projected volume
  mounts every one back at its path under `/app`. The shared role's client-side apply caps each
  object near 256 KiB, and the modules total about 700 KB.
- **`longhorn_robustness.py` stays off the list.** Only the runbook gates import it, from the
  checkout, so it sits in `test_monitor_bridge_modules.py`'s `CHECKOUT_ONLY`, and the test
  fails if a shipped module imports it. The mount-layout sibling test catches a module the
  layout misses.
- **A module edit rolls the pod.** Under Flux the restart annotation that the renderer stamps
  (`RESTART_ANNOTATION` in `scripts/renderer/renderer_lib/restart.py`) hashes every ConfigMap
  the volume names. `scripts/renderer/tests/test_renderer_monitor_bridge_script.py` holds the
  embed, the size and the checksum.
- **Flux applies the role** since #5594. Its `containers_list` entry is `platform: k8s`, and the
  Kustomization's `wait` replaces the rollout wait.
- **Config in tests.** A test states the registry, gate rows first, and its I/O as a
  `FakeSources` passed as `src`. A default argument cannot read the config, because it
  evaluates at import.

## Operator prerequisites

1. No push token. The bridge reads none since A6's Cutover-2.
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

## The module table

| module | holds |
|---|---|
| `cli.py` | the `argparse` front end and `main(argv, env, checks, sources) -> int`, which builds the `Config`, the registry and the `Sources`, refuses an unknown check name in the filter and loops `run_once` |
| `check.py` | `run_once(cfg, src, checks, only, export)` — the run loop, with `GATE_SKIP` (the two gate rows and the message a skipped dependent logs), `EXPORTER_DEPENDENT`, `check_enabled`, `down_exporters` and `_evaluate` |
| `check_table.py` | `CHECKS`, one `PushCheck` per check and gate: its body, gate, `critical` flag and runbook slug |
| `registry.py` | `build_checks()`, a `Check` for every row of `CHECKS`, the two gate rows first, each with its row's `gate` |
| `deploy_pipeline.py` | the deploy-pipeline exporter: `collect(cfg, src, now)` reads `promote`'s state with three `api.github.com` GETs and two `ghcr.io` GETs into four `homelab_deploy_*` gauges, adds `homelab_deploy_reboot_window_active` from the `WEEKLY_REBOOT_*` schedule, and `Exposition` serves them on `DEPLOY_PIPELINE_PORT` from a daemon thread. `cli.main()` starts the listener and calls `refresh` after each cycle |
| `verdict_export.py` | the check exporter: `CheckExposition` (a `deploy_pipeline.Exposition` on its own port, `CHECK_EXPORTER_PORT`) serves `homelab_check_success`, `homelab_check_last_run_timestamp_seconds` and `homelab_check_deadline_seconds` per row and `homelab_check_rows` for the table. `check.run_once` records each row's verdict through `export`; `cli.main()` sets the row count from `check_table.CHECKS` |
| `longhorn_robustness.py` | `SAFE_ROBUSTNESS` and `unsafe_volumes` — which Longhorn robustness states are lost redundancy. Checkout-only: it does not ship in the pod, and `scripts/deploy_tools/runbook_gates_lib/gate_runner.py` imports it from the checkout to judge the CRs (#3668) |
| `bridge/types.py` | `PushCheck`, `Check`, `CheckResult`, `CheckFn` — shared by `registry.py` and `check.py` without either importing the other |
| `checks/<domain>.py` | the `check_*` bodies by domain: `service`, `host_packages`, `notify`, `host_thermal` (Scrutiny), `host_edge`, `b2`, `r2`, `cloudflare_ips`, `healthchecks`, `wan`. `host_edge`'s entry points take the probe function as `tcp_open` so a test injects a port map |
| `bridge/config.py` + `config_{host,service,cluster,io}.py` | the `_env`/`_int`/`_num`/`_env_file` parsers, `class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig)`, `load_config(env)`; one builder per domain. `PVC_EXCLUDE` stays in `config.py` because a repo test greps for it by text |
| `bridge/net.py` | the transport: `_get_json`, `_get_bytes`, `_post_json`, `prom_scalar`, `prom_vector`, `loki_count`, and the selector builder (`origin_sel`). Every helper that reads a URL or the origin pin takes `cfg` FIRST. No check body calls its fetchers; they go through `bridge/sources.py` |
| `bridge/sources.py` | `Sources(cfg)`, every query a gate or check body sends (`prom_scalar`, `prom_vector`, `loki_count`, `get_json`, `get_bytes`, `post_json`). `cli.main()` builds one; `run_once` hands it to every body as `src`. |
| `bridge/msgfmt.py` | `format_down(unit, state, items, details)` — the one grammar for a message naming several things; import-free |
| `bridge/streaks.py` | `State` (every streak counter and probe cache, carried as `src.state`), and `down_streak` (the consecutive-down step) |
| `bridge/common.py` | `_env`, `sanitize` and `clamp_discord` (`DISCORD_MAX`) — the helpers shared verbatim with autofix-bridge's `autofix.py`; its header records what was considered and rejected |
| `bridge/parsing.py` | duration/timestamp parsing, `endpoint_label`, `describe_fetch_failure` |
| `verdicts/<domain>.py` | pure decisions taking every threshold as an argument: `host` (`speedtest_verdict`), `host_smart` (`scrutiny_*`), `service`, `notify`, `storage` (the B2/R2 decisions with the R2 Class A/B/free ACTION LISTS beside them — policy, not configuration) |

## The test seams

- **A test states its configuration.** The `cfg` fixture is `load_config(bridge_env())`, the
  rendered `templates/config/env.yaml.j2`, because a key the template renders has no Python
  default (#3659); narrow it with
  `dataclasses.replace(cfg, X=...)`, or call `load_config(bridge_env(X=...))` when the READ is
  under test.
- **A test states what a check reads.** Every check body and gate probe takes `(cfg, src)`, and
  a test hands it `tests/_fake_sources.py`'s `FakeSources(prom_vector=lambda q: ...)` rather
  than patching `bridge.net` (#3742). An unanswered query raises, so `FakeSources()` proves a
  check does no I/O; `src.queries("prom_vector")` lists the PromQL it sent.
- **A test states where results go and how a request is sent.** `run_once` records each
  verdict through its `export` argument, and a test hands it `tests/_fake_sources.py`'s
  `FakeExport()` and reads `export.records`. Every `bridge.net` request function and `Sources`
  take an `opener` in `urllib.request.urlopen`'s shape, and the SMTP backstop takes its
  `login`, so no test patches `urlopen` or `_smtp_login_ok` (#3938).
- **The loop's clock is a parameter as well.** `cli.main` takes `sleep`, and a test points
  `HEARTBEAT_FILE` at the null device through the env it hands `main`, so no test patches
  `time.sleep`, `bridge.common.touch_heartbeat` or `bridge.common.log` (#3986). No monitor-bridge
  test patches a runtime module, and `ansible/tests/repo/monkeypatch_allowlist.txt` has no row
  for this role.
- **A test that does patch one must patch the module that READS the name, and a module reads
  it qualified** (`bridge.net.prom_vector` at call time, never `from bridge.net import prom_vector`). Getting it
  wrong is silent, so `ansible/tests/services/test_monitor_bridge_modules.py` re-derives every
  patched `(module, name)` pair by AST, and `test_bridge_patch_boundary.py` beside it fails a
  runtime module that from-imports a patched name.
- **Per-check state is a parameter too.** Every streak counter and probe cache is a field of
  `bridge.streaks.State`, which `Sources` builds once, so a check reads `src.state.down_streaks`
  and a fresh `FakeSources` starts zeroed (#3866). A test whose cycles each build their own fake
  hands them one `State`: `FakeSources(state=...)`, or conftest's `state` fixture. Without
  that, a "resets the streak" test passes on a counter that never advanced.
- A fixture goes in `tests/conftest.py`; a helper taking arguments goes in an
  underscore-prefixed, repo-unique module (`_check_gate_helpers.py`), never
  `from conftest import ...`.
