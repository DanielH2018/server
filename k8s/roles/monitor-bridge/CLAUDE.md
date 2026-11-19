# monitor-bridge — every threshold check, served as a Prometheus exporter (k8s)

A stdlib-only Python loop (`files/cli.py`) that runs every registered check each `INTERVAL`
(300 s) and serves each verdict as a `homelab_check_success` gauge, so a threshold breach pages
through the `bridge-exporter.yaml` rules in observability.

**`files/check_table.py`'s `CHECKS` declares every check once**: body and gate, from which the
registry and the gate sets derive. Each check's `verdicts/` function says what it evaluates.
This file states what holds across every check. `docs/monitor-bridge-checks.md` has the
measurement behind each number, and `docs/monitor-bridge-internals.md` the module table, the
two exporters, prerequisites, gate-set membership and test-patching rules.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=monitor-bridge` gates the rollout
- **Image:** `python` (`monitor_bridge_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

**Host:** daniel-box, pinned by `nodeSelector` to `inventory/group_vars/all.yml:k8s_primary_node`
because the `etcd_restore_drill` check reads the drill's stamp directory over a hostPath. Every
source is reached by in-cluster Service name.

**Flux applies it.** Its Secrets substitute from `homelab-cluster-vars`, so a new
credential reaches the pod only after `cluster_vars.py write`
(`scripts/secrets_mgmt/cluster_vars.py:write`, operator-only).

## Gates and hysteresis

Two reachability gate rows run first each cycle and skip their dependents, so one outage pages
once (`k8s/roles/monitor-bridge/tests/test_check_table.py::test_the_gate_rows_are_the_two_gates_the_run_loop_skips_by`).
A skipped check exports 0 and its body is not called
(`k8s/roles/monitor-bridge/tests/test_check.py::test_a_down_gate_skips_its_dependents_and_exports_zero_for_both`),
and Alertmanager's inhibit rules hold its page behind the gate's. A row names its gate in the
`gate` column (`k8s/roles/monitor-bridge/files/bridge/types.py:PushCheck`).

- **A gate has no gate of its own** — a gate that cannot go red is not coverage (#2825;
  `k8s/roles/monitor-bridge/files/check_table.py:CHECKS`).
- **`EXPORTER_DEPENDENT`** suppresses a dead node-exporter's dependents, keyed by scrape job
  (`k8s/roles/monitor-bridge/files/check.py:DECIDED: suppression stays keyed`). A new
  node-exporter scrape job fails
  `k8s/roles/monitor-bridge/tests/test_check_exporters.py::test_every_node_exporter_job_is_mapped_in_exporter_dependent`.
- **The `*_CONSECUTIVE` streaks** (`k8s/roles/monitor-bridge/files/bridge/streaks.py:down_streak`)
  are the per-check hysteresis: a held cycle exports 1 with a `down streak n/N` note, so a
  streak delays a page, never suppresses one. The `startup_grace` rows' rules carry `for: 10m`
  instead, which rides out the weekly reboot
  (`k8s/roles/monitor-bridge/tests/test_bridge_row_rules.py::test_every_rows_rule_follows_its_row`).

## Checks

Per-check rules live in the registry entry, its `verdicts/` function and the checks page.
Three conventions hold across all of them:

- An empty credential or URL disables a check, which stays `up` rather than paging about its
  own configuration
  (`k8s/roles/monitor-bridge/tests/test_check_domain_registration.py::test_an_empty_url_disables_the_check_without_a_read`).
- An unreachable source pages through `k8s/roles/monitor-bridge/files/check.py:_evaluate` unless
  the check names a streak.
- Every threshold, window and streak is an env var in `templates/config/env.yaml.j2`; read the
  template, not the constant
  (`k8s/roles/monitor-bridge/tests/test_config_reads_the_rendered_env.py::test_no_rendered_key_carries_a_second_default_in_python`).
  The `monitor-bridge-env` Secret
  (`k8s/roles/monitor-bridge/defaults/main.yml:render_secrets`) holds only credentials, because Flux's typed patch reads a
  Secret's quoted `"300"` back as a number and refuses it (#5574;
  `scripts/renderer/tests/test_renderer_string_values_census.py::test_no_substituting_directory_holds_a_value_that_unquotes_to_a_non_string`).

**A new arm folds into an existing check by default**; prove its selector against a live source
first. **A new check is one `check_table.py` row and one `check_*` body, plus a rule in
observability's `bridge-exporter.yaml`** for it to page
(`k8s/roles/monitor-bridge/files/check_table.py:CHECKS`).

## The two exporters

`files/verdict_export.py` serves the check verdicts, each down row's failing members and the
domain registration's days left
(`k8s/roles/monitor-bridge/files/verdict_export.py:CheckExposition`), and
`files/deploy_pipeline.py` serves five `homelab_deploy_*` gauges from the `promote` workflow's
state
(`k8s/roles/monitor-bridge/tests/test_deploy_pipeline.py::test_the_endpoint_serves_the_five_gauges_and_nothing_else`).
Each has its own port and scrape job (`k8s/roles/monitor-bridge/files/cli.py:main`). The
internals page has their series and wiring.

- **The check value is the verdict before suppression.** A body that raises exports 0
  (`k8s/roles/monitor-bridge/tests/test_verdict_export.py::test_a_raising_body_exports_zero_and_the_next_row_still_runs`).
  A row with no rule in `bridge-exporter.yaml` never pages.
- **A failed deploy-pipeline read exports no series, never a 0**
  (`k8s/roles/monitor-bridge/tests/test_deploy_pipeline.py::test_a_failed_read_drops_only_the_gauges_it_feeds`).
  Its token (`DEPLOY_PIPELINE_GITHUB_TOKEN`) is read-only on Actions and Variables, so the
  family can neither clear a hold nor move a tag.
- **`--once` and `--check` serve nothing**, so a hand-run beside the pod never contends for a
  port
  (`k8s/roles/monitor-bridge/tests/test_check_cli.py::test_once_publishes_nothing_and_reads_no_github`).

## Module layout

`files/` holds flat modules and three packages: `bridge/` (shared plumbing), `checks/` (one
module per domain of `check_*` bodies) and `verdicts/` (pure logic taking its inputs as
arguments). `check_table.py` imports only `bridge.types` and the `checks.*` bodies; a row's
fields but `fn` are literals, because `scripts/renderer/template_filters/py_table.py` parses the table
without running it.

**Adding a module means adding its path to `monitor_bridge_modules`** in `defaults/main.yml`.
Each entry renders as its own ConfigMap, and a module missing from the list kills the pod at
import on its next roll; `tests/services/test_monitor_bridge_modules.py` catches that.
The internals page says why it is one ConfigMap per module and which module stays off the list.

## Configuration is a parameter, not a module global

`cli.py`'s `main()` calls `load_config(os.environ)` ONCE and hands the frozen `Config` to
`check.run_once`, which passes it to every gate, check body and `bridge.net` helper that reads a
URL (`k8s/roles/monitor-bridge/files/cli.py:main`,
`k8s/roles/monitor-bridge/files/check.py:run_once`). Building the config MUST NOT raise:
`_int`/`_num` record a bad value in `CONFIG_PROBLEMS`, and `main()` exits 2
(`k8s/roles/monitor-bridge/tests/test_check_cli.py::test_main_reports_config_problems_and_exits_two`).
**A key the pod's environment renders has no Python default**
(`k8s/roles/monitor-bridge/tests/test_check_cli.py::test_a_rendered_key_missing_from_the_env_is_recorded_rather_than_defaulted`);
the `cfg` fixture is that render (`k8s/roles/monitor-bridge/tests/conftest.py:cfg`). A
`verdicts/` module reads no `cfg` at all.

## Editing & testing
Unit tests are `uv run pytest k8s/roles/monitor-bridge/tests`, one file per domain.
**A shared test helper goes under `tests/`, never beside `cli.py`** — every `.py` in `files/` is
production code to `tests/services/test_monitor_bridge_modules.py:_runtime_modules`. Smoke test
one pass with
`sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once`
(**operator-only**); `--check <name>` runs a gated check without its gate, which reads as up
(`k8s/roles/monitor-bridge/tests/test_check_cli.py::test_check_flag_names_one_row_without_its_gate`).
A session's `kubectl` cannot exec, so it reads the live pass's result with `probe.py monitors`.
`probe.py monitors --checks [name]` prints each row's value, last-run age and note: a held
streak, a skip behind a down gate, staleness, or a missing rule
(`scripts/diagnostics/probe_lib/monitor_checks.py:report`).

## Traps

- **The Alloy k8s pod streams have no `app` label**, so a selector written from the `-l app=`
  habit matches nothing and a fail-open arm reads that as health.
  `tests/_loki_streams.py:LOKI_STREAM_LABELS` and
  `k8s/roles/monitor-bridge/tests/test_check_loki.py::test_loki_selectors_use_real_stream_labels`
  pin the vocabulary.
- **The runtime stamps the log lines, not `bridge.common.log`**
  (`k8s/roles/monitor-bridge/files/bridge/common.py:log`) — read a `kubectl logs
  --timestamps` prefix as UTC.
