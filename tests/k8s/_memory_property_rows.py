"""Rendered-manifest property rows that join a container's resources with recorded usage.

`test_rendered_properties.py` runs these rows beside its own. The p95 figures come from
`docs/assets/generated/critical-memory-p95.json`, which the docs-refresh cron regenerates from
Prometheus (`scripts/docs/reference/critical_memory.py`), because CI cannot query it.
"""

import functools
import re

from diagnostics.probe_lib.usage import (
    CRITICAL_PRIORITY_CLASS,
    UNDER_REQUEST,
    under_requested,
)
from docs.reference.critical_memory import MUST_RECORD, read_table

from _k8s_render import pod_spec
from _property_table import Property
from _workload_property_rows import POD_KINDS

_MIB = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024}
_QUANTITY = re.compile(r"(\d+)(Ki|Mi|Gi)")
_p95_table = functools.cache(read_table)


def _mib(quantity: str) -> float:
    match = _QUANTITY.fullmatch(str(quantity))
    assert match, f"memory request {quantity!r} is not a Ki/Mi/Gi quantity"
    return int(match[1]) * _MIB[match[2]]


def _critical_containers(role: str, tpl: str, doc: dict):
    if doc.get("kind") not in POD_KINDS:
        return
    spec = pod_spec(doc)
    if spec.get("priorityClassName") != CRITICAL_PRIORITY_CLASS:
        return
    p95s = _p95_table()
    for container in (spec.get("initContainers") or []) + (
        spec.get("containers") or []
    ):
        key = f"{doc['metadata']['namespace']}/{doc['metadata']['name']}/{container['name']}"
        if key in p95s:
            request = ((container.get("resources") or {}).get("requests") or {}).get(
                "memory", "0Mi"
            )
            yield key, {"request_mib": _mib(request), "p95_mib": p95s[key]}


def _under_requested_offence(role: str, usage: dict) -> str | None:
    if not under_requested(usage["request_mib"], usage["p95_mib"]):
        return None
    return (
        f"memory request {usage['request_mib']:.0f}Mi is under {UNDER_REQUEST:.0%} of its "
        f"7-day p95 working set, {usage['p95_mib']}Mi; raise the request to the p95"
    )


MEMORY_PROPERTIES = (
    Property(
        name="critical-tier-memory-request-covers-half-its-p95",
        reason=(
            "A critical-tier pod whose memory request sits far under what it uses lets the "
            "scheduler pack its node as if the memory were free, and a Burstable pod over its "
            "request ranks higher for the kernel OOM killer. `probe.py usage` flags a request "
            "more than 25% off its p95, but only when someone runs it. This row fails a "
            "container of a pod at the critical PriorityClass whose request is under half its "
            "recorded 7-day p95. A container with no recorded p95 (a new workload, or one not "
            "yet at the critical class) passes until the docs-refresh cron records one."
        ),
        select=_critical_containers,
        offence=_under_requested_offence,
        red=("pihole", {"request_mib": 128, "p95_mib": 304}),
        green=("pihole", {"request_mib": 152, "p95_mib": 304}),
        more_green=(("traefik", {"request_mib": 192, "p95_mib": 224}),),
        min_matches=28,
        must_find=MUST_RECORD,
    ),
)
