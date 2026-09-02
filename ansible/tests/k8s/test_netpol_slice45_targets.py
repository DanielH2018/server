"""The slice-4.5 readiness gate must survive a target with no ready endpoints.

Three things have to agree about that probe's targets: the readiness gate in tasks/main.yml,
the assertions in netpol-probe-slice45-job.yaml.j2, and the list they both read. This pins the
agreement, and pins the query shape that made a scaled-to-zero target unreadable.

WHY THE QUERY SHAPE IS A TEST AND NOT A COMMENT. A Service with no ready pods keeps its
EndpointSlice and sets `endpoints: null`, not `[]`. kubectl's jsonpath filter cannot filter nil
— it exits 1 with `<nil> is not array or slice` — so the READ failed before the assert could
name the target. Measured 2026-09-02 against the live cluster: the filter form exits 1 on
terraria (0 replicas) and 0 on freshrss; the `[*]` form exits 0 on both. That cost three failed
`deploy.yml` runs in one evening, each reporting a Go template dump instead of "terraria is
down".
"""

from __future__ import annotations

import yaml
from _helpers import K8S_ROLES

ROLE = K8S_ROLES / "netpol-baseline"
TASKS = (ROLE / "tasks" / "main.yml").read_text()
# The comments explain the query shape they forbid, so a textual guard over the raw file would
# match its own documentation. Check the executable lines only.
TASK_CODE = "\n".join(
    line for line in TASKS.splitlines() if not line.lstrip().startswith("#")
)
PROBE = (ROLE / "templates" / "netpol-probe-slice45-job.yaml.j2").read_text()
TARGETS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())[
    "netpol_baseline_slice45_targets"
]

READ_TASK = "Read the slice-4.5 probe targets' endpoint readiness"


def test_the_readiness_query_does_not_filter_on_a_field_that_can_be_null() -> None:
    """The accepting half: the gate reads `endpoints[*]`, which survives `endpoints: null`."""
    assert "endpoints[*].conditions.ready" in TASK_CODE, (
        "the slice-4.5 readiness query no longer selects endpoints[*].conditions.ready"
    )


def test_a_jsonpath_filter_on_endpoints_is_rejected() -> None:
    """The rejecting half: the pre-fix query shape, verbatim, must not be back.

    A guard that only asserts the good shape is present would pass with both forms in the file.
    """
    assert "endpoints[?(" not in TASK_CODE, (
        "a jsonpath filter over `endpoints` is back in the slice-4.5 gate. It exits 1 with "
        "`<nil> is not array or slice` whenever a target has no ready pods, which kills the "
        "read before the assert can name the target."
    )


def test_the_gate_requires_an_explicit_ready_true() -> None:
    """Readiness is still asserted, not merely presence.

    The query returns booleans now, so a non-empty stdout would pass for a target whose only
    endpoint is `false` — exactly the state the gate exists to catch.
    """
    assert "'true' in item.stdout.split()" in TASK_CODE, (
        "the slice-4.5 assert no longer requires an explicit ready=true"
    )


def test_the_gate_loops_the_shared_target_list() -> None:
    """One source of truth, so the gate and the probe cannot drift apart."""
    assert "netpol_baseline_slice45_targets" in TASK_CODE, (
        "the readiness gate no longer loops netpol_baseline_slice45_targets"
    )
    assert TARGETS, (
        "netpol_baseline_slice45_targets is empty, so the gate checks nothing"
    )


def test_terraria_is_absent_while_it_is_scaled_to_zero() -> None:
    """#836 set terraria to replicas 0; asserting it has a ready endpoint can only fail.

    Paired with the probe check below: the list and the rendered leg have to agree, or the gate
    passes and the probe fails on the same fact.
    """
    terraria = yaml.safe_load(
        (K8S_ROLES / "terraria" / "defaults" / "main.yml").read_text()
    )
    if int(terraria["terraria_k8s_replicas"]) == 0:
        assert "terraria" not in TARGETS, (
            "terraria is scaled to zero but is still a slice-4.5 target, so every full "
            "deploy.yml fails its readiness gate"
        )
    else:
        assert "terraria" in TARGETS, (
            "terraria is running again — put it back in netpol_baseline_slice45_targets so its "
            "open-port leg is covered"
        )


def test_the_probe_leg_is_gated_on_the_same_list() -> None:
    """The open-port leg must not be rendered for a target the gate does not check."""
    assert "{% if 'terraria' in netpol_baseline_slice45_targets %}" in PROBE, (
        "terraria's open-port leg is not gated on the target list, so it renders whether or not "
        "the workload is running"
    )
