"""The `SUBCOMMANDS` table and the `REGISTRY` built from it: probe.py's dispatch and `--list`.

Split out of probe.py, which had grown to 697 lines. `cli_parser.py` holds argparse and
`curl_pipeline.py` the streaming `curl` path; what lives here is metadata only.

The registry carries names, descriptions, each subcommand's backing `probe_lib` module and its
`run_*` callable. `probe.py`'s `main()` builds its `handlers` table from the entries flagged
`"handler"`, so a `run_X(ns) -> int` subcommand is one row here plus its argparse entry in
`cli_parser.py`. Argparse stays in `cli_parser.py`: each subcommand's arguments differ too much
(`ha` alone nests five subparsers) for a row to describe them. A row with no callable streams a
`curl` pipeline through `plan()` in `curl_pipeline.py`.
`scripts/diagnostics/tests/test_probe_registry.py` checks that the registry, the parser and
the `probe_lib` modules agree.

This module defines no `run_*` function of its own, so `lib.cli_registry.package_entry_points`
still reports the same nineteen subcommand backends — the `run_*` names below are imported, and
that function counts only what a module defines.
"""

# `probe_lib` is a namespace package under `scripts/`, and `lib.cli_registry` is a sibling
# directory of `diagnostics/`, so both need `scripts/` on sys.path — a module gets only its
# importer's path otherwise, and pyproject's `pythonpath` is a pytest setting. This has to sit
# ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.alerts import run_alerts
from diagnostics.probe_lib.arr import run_arr
from diagnostics.probe_lib.b2_ledger import (
    run_b2_deletions,
    run_b2_spend,
)
from diagnostics.probe_lib.builder import run_builder
from diagnostics.probe_lib.ha import run_ha, run_ha_state
from diagnostics.probe_lib.flux import run_flux
from diagnostics.probe_lib.gitops_view import run_gitops_state
from diagnostics.probe_lib.lander import run_lander
from diagnostics.probe_lib.landing_blockers import run_landing
from diagnostics.probe_lib.landing_verdicts import run_landing_verdicts
from diagnostics.probe_lib.health import run_health
from diagnostics.probe_lib.longhorn import (
    run_b2_budget,
    run_b2_longhorn,
)
from diagnostics.probe_lib.metrics import run_query
from diagnostics.probe_lib.monitors import run_kuma_drift, run_monitors
from diagnostics.probe_lib.pi_plane import run_pi_containers, run_pi_targets
from diagnostics.probe_lib.push_rules import run_push_rules
from diagnostics.probe_lib.readonly_rbac import run_readonly_rbac
from diagnostics.probe_lib.releases import run_releases
from diagnostics.probe_lib.shed_set import run_shed_set
from diagnostics.probe_lib.takeover import run_takeover
from diagnostics.probe_lib.vip_placement import run_vip_placement
from lib.cli_registry import Registry

# name, one-line description (matches the subparser's `help=`), backing `probe_lib` module
# (None for a subcommand that only ever streams a curl pipeline through `plan()`), backing
# `run_*` callable (None likewise).
SUBCOMMANDS = [
    ("metric", "Prometheus instant query", "metrics", run_query),
    (
        "targets",
        "Prometheus scrape-target health (--pi scopes to daniel-pi)",
        "pi_plane",
        run_pi_targets,
    ),
    (
        "monitors",
        "Kuma down-monitors rollup and firing ingress alerts (exit 0 = all up)",
        "monitors",
        run_monitors,
    ),
    (
        "kuma-drift",
        "declared monitors vs live ones — catches a tile that is gone rather than down, "
        "which `monitors` counts as green (exit 0 = no drift)",
        "monitors",
        run_kuma_drift,
    ),
    ("loki-labels", "Loki label names", None, None),
    ("loki-query", "Loki range query", "metrics", run_query),
    (
        "alerts",
        "monitor-bridge DOWN alert history, collapsed to episodes (Loki)",
        "alerts",
        run_alerts,
    ),
    ("scrutiny", "disk SMART summary", None, None),
    (
        "b2-longhorn",
        "Longhorn backup objects in B2, per volume — proves DATA blocks landed, not just "
        "metadata (exit 1 if any volume has none)",
        "longhorn",
        run_b2_longhorn,
    ),
    (
        "b2-budget",
        "per-shard Class C projection against B2's free-tier daily cap (exit 1 if a weekly "
        "shard is over budget)",
        "longhorn",
        run_b2_budget,
    ),
    (
        "b2-spend",
        "MEASURED Class B spend from Longhorn's own logs (Loki-only, spends nothing on B2)",
        "b2_ledger",
        run_b2_spend,
    ),
    (
        "b2-deletions",
        "charge completed Longhorn backup deletions to the ledger, priced from the last "
        "b2-budget listing (exit 1 when one cannot be priced)",
        "b2_ledger",
        run_b2_deletions,
    ),
    (
        "readonly-rbac",
        "assert plain kubectl is still read-only (exit 1 on privilege creep)",
        "readonly_rbac",
        run_readonly_rbac,
    ),
    (
        "vip-placement",
        "assert every ETP=Local MetalLB VIP has a Ready endpoint on its announcing node "
        "(exit 1 when one is stranded)",
        "vip_placement",
        run_vip_placement,
    ),
    (
        "shed-set",
        "the untiered k8s workloads to scale down when one node is lost, derived from tier",
        "shed_set",
        run_shed_set,
    ),
    (
        "takeover",
        "the field paths Flux's first apply of a tag strips: owned by a manager "
        "kustomize-controller cleans, and absent from the render",
        "takeover",
        run_takeover,
    ),
    (
        "flux",
        "each Flux Kustomization's Ready, applied commit against its source, and suspension "
        "(exit 1 when one is not settled)",
        "flux",
        run_flux,
    ),
    (
        "pi",
        "daniel-pi container view (`pi containers`, one ssh + docker inspect)",
        "pi_plane",
        run_pi_containers,
    ),
    ("cert", "served TLS cert subject/dates", None, None),
    (
        "health",
        "k8s rollout + recent-restart rollup (exit 0 = healthy)",
        "health",
        run_health,
    ),
    ("arr", "read-only *arr API GET (key from SOPS, fed via stdin)", "arr", run_arr),
    ("ha", "Home Assistant live state (read-only, GET)", "ha", run_ha),
    ("ha-state", "live view of the derived HA state model", "ha", run_ha_state),
    (
        "releases",
        "which commit produced each k8s service's applied manifests",
        "releases",
        run_releases,
    ),
    (
        "landing",
        "what would stop a landing now, the runs in flight and the worktrees with their "
        "claims (exit 1 when something blocks)",
        "landing_blockers",
        run_landing,
    ),
    (
        "landing-verdicts",
        "the newest recorded VERDICT line of each landed PR, without grepping task output "
        "(exit 1 when the named PR has none)",
        "landing_verdicts",
        run_landing_verdicts,
    ),
    (
        "gitops-state",
        "every GitOps deployer marker and owed-ledger line with its clear command, "
        "read-only (exit 1 when one is unreadable)",
        "gitops_view",
        run_gitops_state,
    ),
    (
        "push-rules",
        "which GitHub ruleset rule a push of HEAD to a branch meets, and whether this login "
        "bypasses it (exit 1 when a rule refuses the push)",
        "push_rules",
        run_push_rules,
    ),
    (
        "lander",
        "the daniel-server batch lander's newest pass, the landings it started with their "
        "VERDICT, and the PRs it named (exit 1 when the lander is failing or stale)",
        "lander",
        run_lander,
    ),
    (
        "builder",
        "the host-package builder's reason, published versions, newest builds and failures "
        "(exit 1 when it is not publishing)",
        "builder",
        run_builder,
    ),
]

# The subcommands with a callable that `main()` routes by hand instead of through `handlers`,
# because the callable answers only some invocations: `health` takes the container rather than
# the namespace and prints its own `--dry-run`; `targets` calls `run_pi_targets` only with `--pi`;
# `metric`/`loki-query` call `run_query` only without `--json`/`--dry-run`. Every other
# invocation of them streams through `plan()`.
ROUTED_IN_MAIN = frozenset({"health", "targets", "metric", "loki-query"})

# The subcommands that judge something, flagged "verdict": `main()` ends their output with one
# `PROBE-VERDICT:` line derived from the exit code (#4454). `targets` judges only with `--pi`,
# and `ha` only through `VERDICT_HA_SUBCOMMANDS`; `probe.verdict_path` applies both.
VERDICT_SUBCOMMANDS = frozenset(
    {
        "targets",
        "monitors",
        "kuma-drift",
        "alerts",
        "b2-longhorn",
        "b2-budget",
        "b2-deletions",
        "readonly-rbac",
        "vip-placement",
        "flux",
        "pi",
        "health",
        "ha",
        "landing",
        "gitops-state",
        "push-rules",
        "lander",
        "builder",
    }
)
VERDICT_HA_SUBCOMMANDS = frozenset({"verify-automations", "verify-entities"})

REGISTRY = Registry("probe")
for _name, _description, _module, _func in SUBCOMMANDS:
    _flags = ("handler",) if _func is not None and _name not in ROUTED_IN_MAIN else ()
    if _name in VERDICT_SUBCOMMANDS:
        _flags += ("verdict",)
    REGISTRY.add(_name, _func, _description, module=_module, flags=_flags)
