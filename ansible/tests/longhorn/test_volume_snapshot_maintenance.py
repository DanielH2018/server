"""How `k8s/volume-snapshot` handles a DETACHED volume: the maintenance-attach path.

A snapshot of a detached volume never becomes ready, because no engine is running to take
it. The role attaches the volume in maintenance mode (`disableFrontend: true`), retakes the
snapshot, detaches again, and only then decides whether the deploy is protected. Every step
is a Longhorn API call whose outcome folds into one readiness fact, so these guards pin the
folding expressions, the drill-proven order of the sequence, and the warning that fires when
the attach itself fails. The retention and naming decisions are in
`test_volume_snapshot_retention.py`; the deploy-hygiene checks stay in `test_volume_snapshot.py`.
"""

from __future__ import annotations

from _volume_ops import assert_every_api_call_pins_a_single_status_code

import yaml
from _helpers import load_tasks as _tasks
from _helpers import render_expr as _render
from _volume_snapshot import _CLAIM, _DEFAULTS, _GUARD, _named


def _detached_expression() -> str:
    return _named(
        _CLAIM, "Decide whether this claim's unready snapshot is a detached-volume case"
    )["ansible.builtin.set_fact"]["volume_snapshot_detached"]


def _detached(ready_stdout: str, state_stdout: str) -> bool:
    return bool(
        _render(
            _detached_expression(),
            volume_snapshot_ready={"stdout": ready_stdout},
            volume_snapshot_volume_state={"stdout": state_stdout},
        )
    )


def test_a_ready_snapshot_is_never_the_detached_case() -> None:
    assert _detached("true|false", "detached") is False


def test_a_markremoved_snapshot_is_not_the_detached_case() -> None:
    """markRemoved means a name collision with a prior deploy's snapshot, not a missing
    engine — it must still fail the deploy rather than being waved through as a skip."""
    assert _detached("false|true", "detached") is False


def test_an_unready_snapshot_on_an_attached_volume_is_not_the_detached_case() -> None:
    """A stuck engine on an attached volume is a genuine failure, not the scaled-to-zero case
    the skip exists for."""
    assert _detached("false|false", "attached") is False


def test_an_unready_snapshot_on_a_detached_volume_is_the_detached_case() -> None:
    assert _detached("false|false", "detached") is True


def test_an_unread_volume_state_is_treated_as_not_attached() -> None:
    """The volume-state read only runs once the wait has already failed.

    An empty read (rc!=0, or a jsonpath that returned nothing) must not be read as 'attached' and
    fall through to failing the deploy instead of the named skip.
    """
    assert _detached("false|false", "") is True


def _named_when(fragment: str) -> list:
    when = _named(_CLAIM, fragment).get("when", [])
    assert isinstance(when, list), (
        f"{fragment!r}'s when: is not a list — the tests below check list membership, "
        f"which would silently pass on any string containing the guard as a substring"
    )
    return when


def test_the_maintenance_attach_resolves_longhorn_api_with_the_named_entry_point() -> (
    None
):
    """`k8s/longhorn-api`'s tasks/main.yml exists only to fail loudly at a caller who forgets
    `tasks_from` — dropping it while keeping the include is the edit that looks harmless."""
    task = _named(_CLAIM, "Resolve the node-local Longhorn API")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/longhorn-api"
    assert include["tasks_from"] == "resolve.yml"
    when = _named_when("Resolve the node-local Longhorn API")
    assert _GUARD in when
    assert "volume_snapshot_detached | bool" in when


def test_the_maintenance_attach_resolve_uses_soft_mode_not_ignore_errors() -> None:
    """`ignore_errors` on a dynamic `include_role` does NOT catch a failure of a task the
    include pulls in — only a failure of the include statement itself. Round 1 of this task
    shipped exactly that mistake, and a reviewer proved it does nothing by running the real
    (unmodified) k8s/longhorn-api role through a scratch play with `k3s` stubbed to report no
    manager pod: the play still aborted, `ignore_errors` on the include notwithstanding.

    This test only pins the STATIC shape of the fix — `longhorn_api_required: false` passed,
    `ignore_errors` gone. The MECHANISM is proven by
    `test_longhorn_api_soft_mode_survives_no_manager` in test_longhorn_api.py, which runs the
    real role through a scratch play exactly as the reviewer did. A test that only checks a
    keyword here would have been green for the original, broken `ignore_errors` version too —
    that is the failure mode this docstring exists to name."""
    task = _named(_CLAIM, "Resolve the node-local Longhorn API")
    assert "ignore_errors" not in task
    assert task["vars"]["longhorn_api_required"] is False


def test_the_maintenance_attach_requests_disablefrontend_on_this_node() -> None:
    body = _named(_CLAIM, "Attach")["ansible.builtin.uri"]["body"]
    assert body["disableFrontend"] is True
    assert body["hostId"] == "{{ longhorn_api_node }}"
    assert "attachmentID" not in body


def test_the_maintenance_detach_sends_neither_hostid_nor_attachment_id() -> None:
    """Pairs with the attach on the empty ticket key, same as k8s/volume-revert:

    sending `attachmentID` on the attach alone would make this detach remove nothing while still
    returning 200.
    """
    body = _named(_CLAIM, "Detach")["ansible.builtin.uri"].get("body", {})
    assert body == {}


def test_every_maintenance_api_call_pins_a_single_status_code() -> None:
    assert_every_api_call_pins_a_single_status_code(_CLAIM)


def _maintenance_attached_expression() -> str:
    return _named(_CLAIM, "Decide whether the maintenance-mode attach succeeded")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_maintenance_attached"]


def _maintenance_attached(attach_status, wait_state, frontend_failed) -> bool:
    # Every `None` is simulated by the value its own `default(...)` fallback would produce
    # (status 0, empty stdout, failed=True — fail closed), not by omitting the register or the
    # field. `ansible_default` — the filter `FilterModule` actually wires to the name `default`
    # — only coalesces `UndefinedMarker`, Ansible's own type; this harness's plain
    # `NativeEnvironment` produces jinja2's native `Undefined` for both a variable never passed
    # to render() and a missing dict key accessed via `.attr`, and neither is caught. Same
    # caveat `test_the_prune_loop_slices_cleanly_at_the_defaulted_floor_values` documents above.
    # So this pins the DOWNSTREAM decision against the coalesced value, not the coalescing
    # itself.
    attach = {"status": 0 if attach_status is None else attach_status}
    wait = {"stdout": "" if wait_state is None else wait_state}
    frontend = {"failed": True if frontend_failed is None else frontend_failed}
    return bool(
        _render(
            _maintenance_attached_expression(),
            volume_snapshot_maint_attach=attach,
            volume_snapshot_maint_wait=wait,
            volume_snapshot_maint_frontend=frontend,
        )
    )


def test_the_attach_is_only_successful_with_a_200_attached_state_and_a_passed_frontend_assert() -> (
    None
):
    assert _maintenance_attached(200, "attached", frontend_failed=False) is True


def test_a_non_200_attach_response_is_not_success() -> None:
    assert _maintenance_attached(500, "attached", frontend_failed=False) is False


def test_an_attach_that_never_reaches_attached_state_is_not_success() -> None:
    assert _maintenance_attached(200, "attaching", frontend_failed=False) is False


def test_an_attach_that_reaches_attached_with_the_frontend_still_enabled_is_not_success() -> (
    None
):
    """The frontend check is now the separate assert task's `.failed`, not a second field
    parsed inline here — `frontend_failed=True` is what that assert records when
    `disableFrontend` came back `false`."""
    assert _maintenance_attached(200, "attached", frontend_failed=True) is False


def test_a_skipped_attach_attempt_is_not_success() -> None:
    """If `longhorn_api` never resolved, the attach, wait and frontend-assert tasks are all
    skipped and none of the three registers carry a real value. The decision must read that as
    failure — `frontend_failed=None` renders as the assert task's own `default(true)` fallback,
    the same fail-closed default `claim.yml` uses."""
    assert _maintenance_attached(None, None, None) is False


def _detached_refold_expression() -> str:
    return _named(
        _CLAIM,
        "Fold the maintenance-mode attempt back into the detached-volume decision",
    )["ansible.builtin.set_fact"]["volume_snapshot_detached"]


def _refolded_detached(maintenance_attached: bool) -> bool:
    return bool(
        _render(
            _detached_refold_expression(),
            volume_snapshot_maintenance_attached=maintenance_attached,
        )
    )


def test_a_successful_maintenance_attach_clears_the_detached_flag() -> None:
    """Clearing it is what lets the existing 'Fail on a snapshot that never became usable' task
    catch a genuinely stuck engine post-attach, and lets the prune block run once there is a
    real snapshot to prune around."""
    assert _refolded_detached(True) is False


def test_a_failed_maintenance_attach_leaves_the_detached_flag_set() -> None:
    """This is the one case the warning must still fire for — the attach itself failed."""
    assert _refolded_detached(False) is True


def test_the_refold_expression_defaults_a_missing_attach_outcome_to_still_detached() -> (
    None
):
    """The fold task only runs when this claim started detached, so
    `volume_snapshot_maintenance_attached` should always have just been set — but a stale or
    missing value must fail closed (still warn), not silently clear a flag it never earned.

    ONE-TIME OBSERVATION, not a persisting behavioural test — by choice, not because it is
    impossible. `ansible_default` (the filter `FilterModule` wires to the name `default`) only
    coalesces `UndefinedMarker`; this harness's plain `NativeEnvironment` produces jinja2's
    native `Undefined` for a variable never passed to render() at all, which `ansible_default`
    does not catch, so rendering this expression with the variable omitted raises here even
    though real Ansible's `AnsibleUndefined` would coalesce correctly.

    It CAN be made to render: constructing
    `ansible._internal._templating._jinja_common.UndefinedMarker(name=..., _no_template_source=True)`
    and passing that object as the variable's value produces exactly the type `ansible_default`
    checks for, and the expression coalesces the way it would under real Ansible. Not done here
    — that is a private underscore module with an undocumented constructor kwarg, in a part of
    ansible-core that was recently rewritten, and coupling a test to it trades a harness gap for
    a dependency on ansible-core internals that owe this test nothing and can change without
    notice. Pinning the source text is the trade actually made, not the only option available."""
    assert "default(false)" in _detached_refold_expression().replace(" ", "")


def test_the_retake_uses_the_same_snapshot_name_as_the_original_apply() -> None:
    """The prune's 'found this run's own snapshot' assert, and the wait that follows, both key
    on `volume_snapshot_name` — a retake under a different name would make the prune fail and
    the original wait poll for a CR that never gets created."""
    task = _named(_CLAIM, "Retake the pre-deploy snapshot")
    stdin = task["ansible.builtin.command"]["stdin"]
    assert "name: {{ volume_snapshot_name }}" in stdin
    assert "createSnapshot: true" in stdin


def test_the_retaken_snapshot_wait_registers_to_its_own_name() -> None:
    """INVERTED 2026-08-21 by the task-6 drill.

    This test previously asserted the OPPOSITE — that the retake wait reuses `volume_snapshot_ready`
    so the pre-7b fail-task and prune guards "keep working unmodified against whichever attempt
    actually ran". That design does not work, and this test was pinning the bug in place: **a
    skipped task still sets the variable it registers to**, to a result with no `stdout`. On the
    attached path the retake is skipped, so it erased the first wait's real reading and the role
    failed every normal deploy of all thirteen opted-in services.

    Downstream tasks now read the `volume_snapshot_ready_out` fact, which folds the two waits by the
    path actually taken. `test_skipped_retake_wait_keeps_first_read` in
    test_volume_snapshot_register.py is the behavioural half — it runs the role and would have
    caught what this source-text assertion could not.
    """
    task = _named(_CLAIM, "Wait for the retaken snapshot to become usable")
    assert task["register"] == "volume_snapshot_retake_ready"


def test_the_downstream_fail_reads_the_folded_readiness_fact() -> None:
    """The other half of the fix:

    the fail task must read the folded fact rather than either wait's register directly, or the
    clobber comes back the moment someone reorders the file.
    """
    task = _named(_CLAIM, "Fail on a snapshot that never became usable")
    conditions = " ".join(task["when"])
    assert "volume_snapshot_ready_out" in conditions
    assert "volume_snapshot_ready." not in conditions


def test_the_detach_after_maintenance_runs_whenever_the_attach_succeeded_regardless_of_the_snapshot_outcome() -> (
    None
):
    """A snapshot that never became ready after a successful attach must not leave the volume
    attached — a stale attachment ticket costs the NEXT deploy's own maintenance-mode attach a
    full `volume_snapshot_state_timeout` wait before it fails, naming the wrong cause. So the
    detach's `when:` must depend on the attach outcome alone, never on `volume_snapshot_ready`."""
    when = _named_when("Detach")
    assert _GUARD in when
    assert "volume_snapshot_maintenance_attached | bool" in when
    assert not any("volume_snapshot_ready" in str(clause) for clause in when)


def test_the_maintenance_detach_wait_is_never_suppressed() -> None:
    """The only proof that a 200 detach actually detached.

    Suppressing it would let the play continue with the volume attached in maintenance mode.
    """
    task = _named(
        _CLAIM, "Wait for the detach after the maintenance-mode snapshot attempt"
    )
    dumped = yaml.safe_dump(task)
    assert "failed_when: false" not in dumped
    assert "ignore_errors" not in dumped
    assert task["until"].strip().endswith("== 'detached'")


# The full drill-proven order, by unique name fragment. Positional, not per-task — a per-task
# `when:` guard has no way to see where in the sequence it runs, so transposing two adjacent
# tasks (e.g. retaking the snapshot BEFORE the attach — snapshotting a volume never actually put
# into maintenance mode, the precise bug this task exists to prevent) leaves every guard test
# above green. Only a positional check catches it.
_MAINTENANCE_SEQUENCE = (
    "Resolve the node-local Longhorn API",
    "Attach the volume in maintenance mode",
    "Wait for the maintenance-mode attach",
    "Assert the maintenance-mode attach really set disableFrontend",
    "Decide whether the maintenance-mode attach succeeded",
    "Retake the pre-deploy snapshot",
    "Wait for the retaken snapshot to become usable",
    "Detach the volume after the maintenance-mode snapshot attempt",
    "Wait for the detach after the maintenance-mode snapshot attempt",
    "Fold the maintenance-mode attempt back into the detached-volume decision",
    "Warn and skip the snapshot for a detached volume",
)


def _task_index(fragment: str) -> int:
    for i, task in enumerate(_tasks(_CLAIM)):
        if fragment in str(task.get("name", "")):
            return i
    raise AssertionError(fragment)


def test_every_maintenance_attach_task_is_guarded_on_detached_and_no_mutate() -> None:
    for fragment in _MAINTENANCE_SEQUENCE[
        :-1
    ]:  # the warn task's own guard is checked below
        when = _named_when(fragment)
        assert _GUARD in when, fragment
        assert "volume_snapshot_detached | bool" in when, fragment


def test_the_maintenance_sequence_runs_in_the_drill_proven_order() -> None:
    """Mutated 2026-08-21:

    swapping 'Retake' and 'Attach' (retaking before attaching) and, separately, swapping 'Detach'
    and 'Wait for the retaken snapshot' both went red under this test — see the fix-round report for
    the transcript. Every per-task guard test in this file stayed green under both mutations, which
    is why a positional test exists at all.
    """
    positions = [_task_index(fragment) for fragment in _MAINTENANCE_SEQUENCE]
    assert positions == sorted(positions), (
        f"the maintenance-mode sequence ran out of order: "
        f"{list(zip(_MAINTENANCE_SEQUENCE, positions, strict=True))}"
    )


def test_the_role_declares_maintenance_attach_timeouts() -> None:
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert int(defaults["volume_snapshot_state_timeout"]) > 0
    assert int(defaults["volume_snapshot_poll_interval"]) > 0
    assert int(defaults["volume_snapshot_api_timeout"]) > 0


def test_the_warn_task_fires_only_for_the_detached_case() -> None:
    """Checked against the raw `when:` list, not its stringified form.

    `str(["not (volume_snapshot_detached | bool)"])` still contains the substring
    `"volume_snapshot_detached | bool"`, so a substring check here would pass on both polarities
    — inverting the task would make it warn "UNPROTECTED" on every healthy deploy and stay
    silent on the one case that matters, with this test still green.
    """
    when = _named(_CLAIM, "Warn and skip the snapshot for a detached volume").get(
        "when", []
    )
    assert _GUARD in when
    assert "volume_snapshot_detached | bool" in when
    assert "not (volume_snapshot_detached | bool)" not in when


def test_the_warning_names_the_service_the_claim_and_that_the_deploy_is_unprotected() -> (
    None
):
    """A silent skip would defeat the point of the slice — the recovery point's absence has to
    be as visible as its presence."""
    msg = _named(_CLAIM, "Warn and skip the snapshot for a detached volume")[
        "ansible.builtin.debug"
    ]["msg"]
    assert "{{ volume_snapshot_service }}" in msg
    assert "{{ volume_snapshot_claim }}" in msg
    assert "UNPROTECTED" in msg


def test_the_warning_names_the_maintenance_attach_as_the_narrowed_cause() -> None:
    """7a's warning fired for every plainly-detached volume.

    7b tries the maintenance-mode attach first, so by the time this task runs the attach has already
    failed — the message must say that, not the old blanket 'no running engine' framing that is no
    longer why the claim is unprotected on a first deploy (that case now takes a snapshot instead,
    see CLAUDE.md).
    """
    msg = _named(_CLAIM, "Warn and skip the snapshot for a detached volume")[
        "ansible.builtin.debug"
    ]["msg"]
    assert "maintenance-mode attach" in msg
    assert "expected on this service" not in msg.lower()


def test_the_fail_task_does_not_fire_for_the_detached_case() -> None:
    when = str(
        _named(_CLAIM, "Fail on a snapshot that never became usable").get("when", "")
    )
    assert "not (volume_snapshot_detached | bool)" in when


def test_the_prune_block_skips_a_detached_claim_too() -> None:
    """The snapshot for a detached claim was never taken, so the retention listing has nothing
    of this run's to find. Running the prune block anyway would fail the 'found this run's
    snapshot' assert for a claim that was correctly, deliberately skipped."""
    for fragment in (
        "List this service's live snapshots",
        "Choose which older snapshots to prune",
        "Check that the snapshot listing found this run's snapshot",
        "Prune snapshots beyond the retention window",
    ):
        when = str(_named(_CLAIM, fragment).get("when", ""))
        assert _GUARD in when, fragment
        assert "not (volume_snapshot_detached | bool)" in when, fragment
