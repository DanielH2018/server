#!/usr/bin/env python3
"""Whether the staging egress fence FIRES, measured the way the monthly drill measures it.

test_staging_egress_fence.py holds the filter's shape and attachment, which is the ceiling of
what a check that never leaves the repo can see: a libvirt nwfilter that is defined and
unattached reads identical to a working one from the host. The leg that closes that gap dials
every fenced range plus an internet control target from INSIDE the drill guest, before the guest
is handed the cluster token or the R2 write credentials (#2943).

Each test here is one verdict of that leg, driven through _fence_probe's stub dials — so every
verdict has an input that must produce it, and the leg cannot quietly stop distinguishing them.
The fence's own accept/reject pair is the first two: a fence that holds, and the 2026-08-27
measurement that made the fence necessary.

Run: uv run pytest ansible/tests/staging/test_staging_egress_fence_fires.py
"""

from _fence_probe import (
    CONTROL,
    POD_IP,
    _all_vars,
    _load_host_vars,
    host_reaches,
    run_fence,
    state,
)


def test_a_fence_that_holds_reports_hold_and_records_its_evidence(tmp_path):
    """The input it must ACCEPT: the control answers, every production target is refused.

    This is #2943's Verify-by in miniature — the evidence names each fenced range refused and
    the internet control target reachable.
    """
    verdict, lines = run_fence(
        tmp_path, guest_reachable=[CONTROL], host_reachable=host_reaches()
    )

    assert verdict == "VERDICT hold", verdict
    assert state(lines, "INTERNET").startswith("REACHABLE"), lines
    for line in (l for l in lines if not l.startswith("INTERNET")):
        assert "refused" in line, f"a production target was not refused: {line}"
    assert {l.split()[0] for l in lines} == {
        "INTERNET",
        "PRODVIP",
        "K3SAPI",
        "WGEASY",
        "LONGHORNSVC",
        "PODNET",
    }, f"the evidence does not name the targets this leg is supposed to dial: {lines}"


def test_a_production_target_answering_from_the_guest_aborts_the_run(tmp_path):
    """REJECT: the 2026-08-27 measurement, which is what the fence was added for.

    wg-easy's admin UI is unauthenticated and LAN-only, so reaching it from the guest is the
    leak. It has to abort rather than note it — the next thing the orchestrator does is hand
    that guest the cluster token and the R2 write credentials.
    """
    pi = _load_host_vars("daniel-pi")["server_ip"]
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL, f"http://{pi}:51821"],
        host_reachable=host_reaches(),
    )

    assert verdict.startswith("FAIL egress fence BROKEN"), verdict
    assert "WGEASY" in verdict, verdict
    assert "Nothing was staged" in verdict, verdict
    assert state(lines, "WGEASY").startswith("REACHABLE"), lines


def test_a_guest_with_no_egress_at_all_is_a_broken_fence_not_a_pass(tmp_path):
    """REJECT: the shape that makes every other assertion here meaningless.

    A rule that lost its destination drops everything, so every production target is refused and
    the run reads like a perfect pass. The control target is the only thing telling the two
    apart, which is why a lost control is a failure rather than a clean sweep.
    """
    verdict, lines = run_fence(tmp_path, guest_reachable=[])

    assert verdict.startswith("FAIL egress fence UNPROVEN"), verdict
    assert "internet control target" in verdict, verdict
    assert state(lines, "INTERNET").startswith("refused"), lines


def test_a_target_that_answers_from_neither_side_is_named_and_not_fatal(tmp_path):
    """A ClusterIP that moved is a probe-maintenance fault, not a leak and not a drill failure.

    Staleness only degrades the meaning of a NEGATIVE: a target answering from inside the guest
    is a leak whether or not it is stale. So the run continues with the label in its verdict,
    and the orchestrator's DECIDED comment carries the trade.
    """
    vip = _all_vars()["k3s_metallb_ingress_vip"]
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL],
        # Everything daniel-server can still reach EXCEPT the Longhorn ClusterIP, which is what
        # a Service that moved to a new address looks like from this host.
        host_reachable=[CONTROL, f"http://{vip}", POD_IP],
    )

    assert verdict == "VERDICT hold,unproven=LONGHORNSVC", verdict
    assert "stale" in state(lines, "LONGHORNSVC"), lines
    assert "refused" in state(lines, "PRODVIP"), (
        "a target the host itself can still reach was proven, so it must read as refused rather "
        f"than stale: {lines}"
    )


def test_a_dial_whose_tool_is_missing_reads_as_unproven_not_refused(tmp_path):
    """This issue's own bug shape, one level down.

    A guest without `ping` refuses nothing — it cannot dial at all. Counting that as a block is
    how a gate reports a fence it never tested, so it lands in the same `unproven` list as a
    stale address rather than in the refused column.
    """
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL],
        host_reachable=host_reaches(),
        guest_tools=("curl",),
    )

    assert verdict == "VERDICT hold,unproven=PODNET", verdict
    assert "UNPROVEN" in state(lines, "PODNET"), lines


def test_a_pod_cidr_with_no_neighbour_to_dial_is_unproven_rather_than_absent(tmp_path):
    """REJECT: the failure a skipped target hides.

    The pod target is the one address the leg discovers at runtime, so on a bridge whose
    neighbour table holds nothing usable there is no address to dial. Dropping the row would
    leave no evidence line and no label, and the run would report a clean `hold` with the pod
    CIDR never measured — this issue's own failure, one level down.
    """
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL],
        host_reachable=host_reaches(),
        pod_ip="",
    )

    assert verdict == "VERDICT hold,unproven=PODNET", verdict
    assert "no usable neighbour" in state(lines, "PODNET"), lines
