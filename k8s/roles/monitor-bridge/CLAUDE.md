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

**Host:** daniel-box, pinned by `nodeSelector` because the `etcd_restore_drill` check reads the
drill's stamp directory over a hostPath. Every source is reached by in-cluster Service name.

**Flux applies it, not Ansible.** Its Secrets substitute from `homelab-cluster-vars`, so a new
credential reaches the pod only after `cluster_vars.py write` (operator-only).

## Gates and hysteresis

Three reachability gate rows run first each cycle and skip their dependents, so one outage pages
once. A skipped check exports 0 and its body is not called, and Alertmanager's inhibit rules
hold its page behind the gate's. A row names its gate in the `gate` column.

- **A gate has no gate of its own** — a gate that cannot go red is not coverage (#2825).
- **`EXPORTER_DEPENDENT`** suppresses a dead node-exporter's dependents, keyed by scrape job
  (`DECIDED:` at the probe in `files/check.py`). A new scrape job fails
  `test_every_node_exporter_job_is_mapped_in_exporter_dependent`.
- **The `*_CONSECUTIVE` streaks** (`bridge/streaks.py`) are the per-check hysteresis: a held
  cycle exports 1 with a `down streak n/N` note, so a streak delays a page, never suppresses
  one. The `startup_grace` rows' rules carry `for: 10m` instead, which rides out the weekly
  reboot.

## Checks

Per-check rules live in the registry entry, its `verdicts/` function and the checks page.
Three conventions hold across all of them:

- An empty credential or URL disables a check, which stays `up` rather than paging about its
  own configuration.
- An unreachable source pages through `_evaluate` unless the check names a streak.
- Every threshold, window and streak is an env var in `templates/config/env.yaml.j2`; read the
  template, not the constant. `templates/env-secret.yaml.j2` holds only credentials, because
  Flux's typed patch reads a Secret's quoted `"300"` back as a number and refuses it (#5574).

**A new arm folds into an existing check by default**; prove its selector against a live source
first. **A new check is one `check_table.py` row and one `check_*` body, plus a rule in
observability's `bridge-exporter.yaml`** for it to page.

## The two exporters

`files/verdict_export.py` serves the check verdicts and the domain registration's days left, and `files/deploy_pipeline.py` serves five
`homelab_deploy_*` gauges from the `promote` workflow's state. Each has its own port and scrape
job. The internals page has their series and wiring.

- **The check value is the verdict before suppression.** A body that raises exports 0. A row
  with no rule in `bridge-exporter.yaml` never pages.
- **A failed deploy-pipeline read exports no series, never a 0.** Its token
  (`DEPLOY_PIPELINE_GITHUB_TOKEN`) is read-only on Actions and Variables, so the family can
  neither clear a hold nor move a tag.
- **`--once`, `--dry-run` and `--check` serve nothing**, so a hand-run beside the pod never
  contends for a port.

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
URL. Building the config MUST NOT raise: `_int`/`_num` record a bad value in
`CONFIG_PROBLEMS`, and `main()` exits 2. **A key the pod's environment renders has no Python
default**; the `cfg` fixture is that render. A `verdicts/` module reads no `cfg` at all.

## Editing & testing
Unit tests are `uv run pytest k8s/roles/monitor-bridge/tests`, one file per domain.
**A shared test helper goes under `tests/`, never beside `cli.py`** — every `.py` in `files/` is
production code to `_runtime_modules()`. Smoke test one pass with
`sudo k3s kubectl -n homelab exec deploy/monitor-bridge -- python /app/cli.py --once`;
`--check <name>` refuses a gated check without its gate.

## Traps

- **The Alloy k8s pod streams have no `app` label**, so a selector written from the `-l app=`
  habit matches nothing and a fail-open arm reads that as health. `LOKI_STREAM_LABELS` and
  `test_loki_selectors_use_real_stream_labels` pin the vocabulary.
- **The runtime stamps the log lines, not `bridge.common.log`** — read a `kubectl logs
  --timestamps` prefix as UTC.
