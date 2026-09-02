"""The order `k8s/volume-revert` runs its steps in, and the shape of each Longhorn call.

The sequence is drill-proven, not chosen (measured 2026-08-21 on `speedtest-config`, Longhorn
v1.12.1): a revert with the frontend enabled returns HTTP 500, and so does a revert on a
plainly detached volume, because no engine is running to perform it. So the volume is
attached in maintenance mode, every step that can fail fails BEFORE the workload is scaled to
zero, and the post-revert detach is verified by state. Each ordering assert here pins a unique
task by name; `_index` refuses an ambiguous match so a rename cannot satisfy it by accident.
"""

from __future__ import annotations

import yaml
from _volume_ops import assert_every_api_call_pins_a_single_status_code
from _volume_revert import _CLAIM, _GUARD, _MAIN, _guard_of, _index, _named, _task_names


def test_the_revert_asserts_the_frontend_is_disabled_before_reverting() -> None:
    """Measured 2026-08-21: a revert with the frontend enabled returns HTTP 500.

    The server answers `failed to revert snapshot for volume ... with frontend enabled`. The assert
    is the precondition, not a formality — without it the revert fails late, after the workload is
    already scaled to zero, leaving the service down AND unreverted.
    """
    tasks = _task_names(_CLAIM)
    assert _index(tasks, "disableFrontend") < _index(tasks, "Revert the volume")


def test_a_missing_snapshot_fails_rather_than_skipping() -> None:
    """A rollback that silently finds no snapshot and proceeds is the exact bug this slice
    exists to fix: the deploy would restore the old manifests against migrated data. Skipping
    is the fail-open direction and must not be reachable."""
    task = _named(_CLAIM, "Fail when no snapshot matches this deploy")
    assert "ansible.builtin.fail" in task
    assert "failed_when: false" not in yaml.safe_dump(task)
    assert "volume_revert_candidates | length == 0" in _guard_of(task)


def test_a_dry_run_reports_a_missing_snapshot_instead_of_aborting() -> None:
    """The failure above is guarded, and something must cover the other half.

    `--check` and `--dry-run` run the two reads for real, so a service that has never deployed
    legitimately has no snapshot — and an unguarded `fail` would abort the dry run rather than
    answer its question. The pair is: fail when mutating, report when not.
    """
    fail = _guard_of(_named(_CLAIM, "Fail when no snapshot matches this deploy"))
    report = _guard_of(_named(_CLAIM, "Report a dry run with nothing to revert"))
    assert _GUARD in fail
    assert "k8s_no_mutate | bool" in report
    assert _GUARD not in report
    assert "volume_revert_candidates | length == 0" in report


def test_the_snapshot_lookup_precedes_the_scale_down() -> None:
    """The frontend assert is not the only step whose lateness costs an outage.

    "No snapshot matches this deploy" is a legitimate outcome — the service's first deploy takes no
    snapshot at all — and reaching it after `--replicas=0` leaves the workload down with nothing to
    revert to. The lookup and its failure both belong upstream of the scale-down.
    """
    tasks = _task_names(_CLAIM)
    assert _index(tasks, "Fail when no snapshot matches this deploy") < _index(
        tasks, "to zero replicas"
    )


def test_the_whole_sequence_is_in_the_drill_proven_order() -> None:
    """Every step of claim.yml, pinned as one sequence.

    Pairwise asserts leave the pairs nobody thought of unpinned, and two of those transpositions
    are outages. Measured 2026-08-21: moving the scale-down AFTER the maintenance-mode attach
    left every other test green, and at runtime the pod still holds the volume, so the attach
    cannot give the engine a disabled frontend — service down, unreverted. Moving the detach
    BEFORE the revert is the same shape: the revert then hits a plainly detached volume and gets
    the drill-measured HTTP 500, again with the workload already at zero.

    The sequence must also be exhaustive: a task in the file and not in this list fails here,
    which is what makes someone adding a step decide where it belongs.
    """
    expected = [
        "Resolve the Longhorn volume backing",
        "Check the Longhorn volume binding",
        "Name the snapshot prefix",
        "taken by this deploy",
        "Choose the newest matching snapshot",
        "Fail when no snapshot matches this deploy",
        "Report a dry run with nothing to revert",
        "Record the snapshot to revert to",
        "to zero replicas",
        "the detach that precedes the attach",
        "in maintenance mode",
        "maintenance-mode attach of",
        "disableFrontend",
        "Revert the volume",
        "Detach the volume",
        "the detach after the revert",
    ]
    names = _task_names(_CLAIM)
    assert len(names) == len(expected), (
        f"claim.yml has {len(names)} tasks and this sequence names {len(expected)}. A step that "
        f"is not in the list is a step whose position nothing checks — add it where it belongs."
    )
    positions = [_index(names, fragment) for fragment in expected]
    assert positions == sorted(positions), (
        "claim.yml's tasks are not in the drill-proven order. Read the table in the role's "
        f"CLAUDE.md before reordering. Positions found: {dict(zip(expected, positions, strict=True))}"
    )
    assert positions == list(range(len(expected)))


def test_the_api_resolve_precedes_the_first_claim() -> None:
    """`longhorn_api` is resolved once in main.yml.

    Resolving it inside claim.yml, or after the loop, would put a failure that has nothing to do
    with this service (no longhorn-manager on this node) downstream of the scale-down.
    """
    tasks = _task_names(_MAIN)
    assert _index(tasks, "Resolve the node-local Longhorn API") < _index(
        tasks, "Revert every volume"
    )


def test_the_api_resolve_names_the_resolve_entry_point() -> None:
    """`k8s/longhorn-api`'s `tasks/main.yml` exists only to fail loudly at a caller who forgets
    `tasks_from`. A bare include therefore aborts the play — and dropping `tasks_from` while
    keeping the include is the exact edit that looks like a simplification."""
    task = _named(_MAIN, "Resolve the node-local Longhorn API")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/longhorn-api"
    assert include["tasks_from"] == "resolve.yml"


def test_neither_attach_nor_detach_sends_an_attachment_id() -> None:
    """The two calls pair on the attachment ticket's key, and the key is the empty string.

    Read from longhorn-manager v1.12.1: `manager.Attach` stores the ticket under the
    `attachmentID` the caller sent, and `manager.Detach` does `delete(tickets, attachmentID)`
    and IGNORES `hostId` entirely. Sending an `attachmentID` on the attach alone therefore
    makes the detach delete nothing — and return HTTP 200 while doing it, leaving the volume
    attached with its frontend disabled and the workload at zero. Both calls omit it, so both
    key the ticket `""`.
    """
    for fragment in ("in maintenance mode", "Detach the volume"):
        body = _named(_CLAIM, fragment)["ansible.builtin.uri"].get("body", {})
        assert "attachmentID" not in body, (
            f"{fragment!r} sends an attachmentID; the attach and the detach must agree on the "
            f"ticket key, and only the empty key is drill-proven."
        )


def test_the_detach_does_not_pretend_hostid_matters() -> None:
    """`manager.Detach` at v1.12.1 accepts `hostId` and never reads it.

    Sending it documents a guarantee the server does not provide, and the next reader would take the
    detach for host-scoped when it is ticket-scoped.
    """
    body = _named(_CLAIM, "Detach the volume")["ansible.builtin.uri"].get("body", {})
    assert "hostId" not in body


def test_the_attach_requests_maintenance_mode_on_this_node() -> None:
    """`disableFrontend: true` is what makes the revert possible at all.

    `hostId` is what keeps the attach on the node whose manager is answering.
    """
    body = _named(_CLAIM, "in maintenance mode")["ansible.builtin.uri"]["body"]
    assert body["disableFrontend"] is True
    assert body["hostId"] == "{{ longhorn_api_node }}"


def test_every_api_call_pins_a_single_status_code() -> None:
    assert_every_api_call_pins_a_single_status_code(_CLAIM)


def test_the_post_revert_detach_is_verified_by_state() -> None:
    """The detach returns 200 whether or not it removed a ticket, so its own status proves nothing.

    The wait on `state: detached` is the only thing that catches a detach that did not detach, and
    suppressing its failure would restore the silence.
    """
    task = _named(_CLAIM, "the detach after the revert")
    dumped = yaml.safe_dump(task)
    assert "failed_when: false" not in dumped
    assert "ignore_errors" not in dumped
    assert task["until"].strip().endswith("== 'detached'")
