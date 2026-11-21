#!/usr/bin/env python3
"""Generates docs/assets/generated/critical-memory-p95.json, the critical tier's memory p95.

CI cannot query Prometheus, so the census that fails a critical-tier container whose memory
request is under half its 7-day p95 working set reads its p95 figures from this table
(`tests/k8s/_memory_property_rows.py`). The docs-refresh cron runs this generator twice a day
from daniel-box, where Prometheus answers, and commits the table when a figure moves.

A row is one container of a pod that ran at `CRITICAL_PRIORITY_CLASS` in the window, keyed
`namespace/workload/container`. Its value is the highest p95 of any pod of the workload, in
MiB rounded up to a multiple of `STEP_MIB`. The rounding keeps the table from changing on
every run, which would make every cron run open a docs-refresh PR.

A query that answers without the members the census names writes nothing, so the previous
table stays in place.

Usage::

    uv run python scripts/docs/reference/critical_memory.py \\
        --out docs/assets/generated/critical-memory-p95.json
"""

import json
import math
from pathlib import Path as _Path

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health_alerts import prom_query
from diagnostics.probe_lib.usage import CRITICAL_P95_QUERY, by_container
from lib.cli_help import ArgumentParser
from lib.docs_provenance import finish_generator
from lib.repo_paths import REPO

SOURCE = "scripts/docs/reference/critical_memory.py"
TABLE = REPO / "docs/assets/generated/critical-memory-p95.json"
STEP_MIB = 16
MUST_RECORD = frozenset(
    {
        "homelab/pihole/pihole",
        "homelab/pihole-2/pihole",
        "homelab/traefik/traefik",
        "homelab/crowdsec-node-agent/crowdsec-agent",
    }
)


def p95_table(series) -> dict[str, int]:
    """`{"namespace/workload/container": p95 MiB}` from `CRITICAL_P95_QUERY`'s result. Pure.

    Raises:
        core.Unreadable: a member of `MUST_RECORD` has no series.
    """
    table = {
        "/".join(key): STEP_MIB * math.ceil(value / 2**20 / STEP_MIB)
        for key, value in by_container(series).items()
    }
    missing = sorted(MUST_RECORD - set(table))
    if missing:
        raise core.Unreadable(
            f"{CRITICAL_P95_QUERY} returned no series for {', '.join(missing)}"
        )
    return dict(sorted(table.items()))


def render(table: dict[str, int]) -> str:
    return json.dumps({"generated_from": SOURCE, "p95_mib": table}, indent=2) + "\n"


def read_table(path: _Path = TABLE) -> dict[str, int]:
    """The committed table's `{"namespace/workload/container": p95 MiB}`."""
    return json.loads(path.read_text())["p95_mib"]


def main(argv: list[str] | None = None, query=prom_query) -> int:
    """Write the table to `--out`; `query` returns an instant query's result list."""
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=_Path, required=True, help="output file path")
    args = parser.parse_args(argv)
    table = p95_table(query(CRITICAL_P95_QUERY))
    return finish_generator(
        "docs.reference.critical_memory", args.out, table, render, "container"
    )


if __name__ == "__main__":
    raise SystemExit(main())
