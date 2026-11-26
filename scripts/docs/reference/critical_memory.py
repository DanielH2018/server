#!/usr/bin/env python3
"""Generates docs/assets/generated/critical-memory-p95.json, the tiered pods' memory p95.

CI cannot query Prometheus, so the census that fails a tiered container whose memory
request is under half its 7-day p95 working set reads its p95 figures from this table
(`tests/k8s/rendered_properties/_memory_property_rows.py`). The docs-refresh cron runs this generator twice a day
from daniel-box, where Prometheus answers, and commits the table when a figure moves.

A row is one container of a pod that ran at a `CENSUS_PRIORITY_CLASSES` class in the window,
keyed `namespace/workload/container`. Its value is the highest p95 of any pod of the workload,
in MiB rounded up to a multiple of `STEP_MIB`. The rounding keeps the table from changing on
every run, which would make every cron run open a docs-refresh PR.

A query that answers without the members the census names writes nothing, so the previous
table stays in place.

A container whose fresh p95 would fail the census against its live request keeps its previous
figure, or no row when it had none, and the generator exits 1 naming it. The docs-refresh run
then publishes everything else and reads DOWN, so one container's drift raises the cron's alert
without holding back every other generated page.

Usage::

    uv run python scripts/docs/reference/critical_memory.py \\
        --out docs/assets/generated/critical-memory-p95.json
"""

import json
import math
import sys
from pathlib import Path as _Path

from diagnostics.probe_lib import core
from diagnostics.probe_lib.health_alerts import prom_query
from diagnostics.probe_lib.usage import (
    CRITICAL_P95_QUERY,
    REQUEST_QUERY,
    by_container,
    under_requested,
)
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
        "homelab/zigbee2mqtt/zigbee2mqtt",
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


def hold_drifted(
    fresh: dict[str, int], previous: dict[str, int], requests_mib: dict[str, float]
) -> tuple[dict[str, int], list[str]]:
    """`fresh` with each drifted container's previous figure, and the drifted keys. Pure.

    A container is drifted when its live request in `requests_mib` is under half its `fresh`
    p95. It keeps its `previous` figure, or leaves the table when it had none.
    """
    table, drifted = {}, []
    for key, p95 in fresh.items():
        request = requests_mib.get(key)
        if request is not None and under_requested(request, p95):
            drifted.append(key)
            if key in previous:
                table[key] = previous[key]
        else:
            table[key] = p95
    return table, drifted


def render(table: dict[str, int]) -> str:
    return json.dumps({"generated_from": SOURCE, "p95_mib": table}, indent=2) + "\n"


def read_table(path: _Path = TABLE) -> dict[str, int]:
    """The committed table's `{"namespace/workload/container": p95 MiB}`."""
    return json.loads(path.read_text())["p95_mib"]


def main(argv: list[str] | None = None, query=prom_query) -> int:
    """Write the table to `--out`; 1 when a container drifted, 0 otherwise.

    `query` returns an instant query's result list.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=_Path, required=True, help="output file path")
    args = parser.parse_args(argv)
    requests_mib = {
        "/".join(key): value / 2**20
        for key, value in by_container(query(REQUEST_QUERY)).items()
    }
    previous = read_table(args.out) if args.out.is_file() else {}
    table, drifted = hold_drifted(
        p95_table(query(CRITICAL_P95_QUERY)), previous, requests_mib
    )
    finish_generator(
        "docs.reference.critical_memory", args.out, table, render, "container"
    )
    if not drifted:
        return 0
    print(
        "docs.reference.critical_memory: memory request under half the 7-day p95, kept the "
        f"previous figure; raise the request: {', '.join(drifted)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
