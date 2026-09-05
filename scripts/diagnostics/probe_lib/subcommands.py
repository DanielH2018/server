"""The `SUBCOMMANDS` table and the `REGISTRY` built from it, for `probe.py --list`.

Split out of probe.py, which had grown to 697 lines. `cli_parser.py` holds argparse and
`curl_pipeline.py` the streaming `curl` path; what lives here is metadata only.

The registry carries names, descriptions and each subcommand's backing `probe_lib` module. It
does NOT dispatch: `plan()` in `curl_pipeline.py` and the `handlers` table in `probe.py`'s `main()`
still own that, unchanged. `scripts/diagnostics/tests/test_probe_registry.py` is the
completeness guard that reads it.

This module defines no `run_*` function of its own, so `lib.cli_registry.package_entry_points`
still reports the same twelve subcommand backends — the `run_*` names below are imported, and
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
    run_b2_record,
    run_b2_spend,
)
from diagnostics.probe_lib.ha import run_ha, run_ha_state
from diagnostics.probe_lib.health import run_health
from diagnostics.probe_lib.longhorn import (
    run_b2_budget,
    run_b2_longhorn,
    run_longhorn_blocks,
)
from diagnostics.probe_lib.metrics import run_query
from diagnostics.probe_lib.monitors import run_kuma_drift, run_monitors
from diagnostics.probe_lib.pi_plane import run_pi_containers, run_pi_targets
from diagnostics.probe_lib.readonly_rbac import run_readonly_rbac
from diagnostics.probe_lib.releases import run_releases
from diagnostics.probe_lib.vip_placement import run_vip_placement
from lib.cli_registry import Registry

# name, one-line description (matches the subparser's `help=`), backing `probe_lib` module
# (None for a subcommand that only ever streams a curl pipeline through `plan()`), backing
# `run_*` callable (None likewise). `REGISTRY` below is built from this and exists for
# `--list` and the completeness guard — see this module's docstring for what it is NOT:
# dispatch stays in `plan()`/`handlers` in `main()`, untouched.
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
        "Kuma down-monitors rollup (exit 0 = all up)",
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
        "b2-record",
        "record a tool's B2 transaction spend in today's ledger",
        "b2_ledger",
        run_b2_record,
    ),
    (
        "b2-deletions",
        "charge completed Longhorn backup deletions to the ledger, priced from the last "
        "b2-budget listing (exit 1 when one cannot be priced)",
        "b2_ledger",
        run_b2_deletions,
    ),
    (
        "longhorn-blocks",
        "census live Longhorn volumes by tier and backup block size (exit 1 when a weekly-"
        "shard volume is not on 16 MiB blocks)",
        "longhorn",
        run_longhorn_blocks,
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
        "pi",
        "Pi glances API (`pi containers` for a one-ssh docker inspect view)",
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
]

REGISTRY = Registry("probe")
for _name, _description, _module, _func in SUBCOMMANDS:
    REGISTRY.add(_name, _func, _description, module=_module)
