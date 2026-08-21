"""What `k8s/volume-snapshot` retains, and what it refuses to delete.

The role's whole value is a negative claim — "if this deploy eats the data, there is a way
back" — and every failure mode is silent. A snapshot that was never taken, a prune that deleted
the newest, a listing that read nothing and therefore pruned nothing: all three leave a green
deploy and an operator who finds out during an incident.

So these tests pin the two decisions that can be wrong without anyone noticing:

  * **the retention window** — newest-first, `markRemoved` CRs excluded from the count, and the
    newest never a candidate whatever `volume_snapshot_retain` says;
  * **the name/prefix coupling** — the prune selects on `volume_snapshot_prefix`, so a snapshot
    named without that prefix would be invisible to its own retention pass and accumulate
    forever.

**These tests exercise the decisions, not the deploy.** `kubectl` in this repo authenticates as
a read-only ServiceAccount and Ansible is the only write path to the cluster, so no snapshot can
be created here. Whether a hand-applied Snapshot CR with `createSnapshot: true` actually produces
a snapshot is **unexercised** and nothing below should be read as covering it.

**Where the synthetic payload enters.** `test_the_listing_jsonpath_parses` runs the role's own
argv against the live API server, so the `stdout_lines` the retention tests inject enter at the
seam the real ones do. That test is not decoration: `test_cronjob_gate_decision.py` records the
sibling case where synthetic payloads injected downstream of a broken `cmd:` string left an
entire branch dead while every test passed.

`split` and `match` are pulled from Ansible's own plugins rather than reimplemented, so the
expressions render against the same code Ansible runs. `max` and `equalto` are Jinja2 builtins
and are already present. The remaining divergence is `jinja2.nativetypes` returning real Python
objects where Ansible renders "True"/"False" strings — which the role's `| int` and `| bool`
coercions collapse identically.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.test.core import TestModule as _AnsibleTests
from jinja2.nativetypes import NativeEnvironment

_ROLE = Path(__file__).resolve().parents[2] / "ansible/roles/k8s/volume-snapshot"
_CLAIM = _ROLE / "tasks/claim.yml"
_MAIN = _ROLE / "tasks/main.yml"
_DEFAULTS = _ROLE / "defaults/main.yml"
_MANIFESTS = _ROLE.parent / "manifests/tasks/main.yml"

_GUARD = "not (k8s_no_mutate | bool)"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _named(path: Path, fragment: str) -> dict:
    for task in _tasks(path):
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError(
        f"no task in {path.name} whose name contains {fragment!r} — the task was renamed or "
        f"removed, and these tests would otherwise silently check nothing."
    )


def _env() -> NativeEnvironment:
    env = NativeEnvironment()
    env.filters.update(FilterModule().filters())
    env.tests.update(_AnsibleTests().tests())
    return env


def _render(expression: str, **context):
    return _env().from_string(expression).render(**context)


# The retention expressions, read out of the live role by task name rather than copied here. A
# rename fails the extraction loudly; a copy would drift silently.
def _live_expression() -> str:
    return _named(_CLAIM, "Choose which older snapshots to prune")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_live"]


def _keep_expression() -> str:
    return _named(_CLAIM, "Choose which older snapshots to prune")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_keep"]


def _stale_expression() -> str:
    return _named(_CLAIM, "Prune snapshots beyond the retention window")["loop"]


def _prune(
    lines: list[str], retain: int, prefix: str = "autodeploy-widget-"
) -> list[str]:
    """The role's real retention decision, end to end, over a synthetic listing."""
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix=prefix,
    )
    keep = _render(_keep_expression(), volume_snapshot_retain=retain)
    return _render(
        _stale_expression(), volume_snapshot_live=live, volume_snapshot_keep=keep
    )


def _kept(
    lines: list[str], retain: int, prefix: str = "autodeploy-widget-"
) -> list[str]:
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix=prefix,
    )
    return [name for name in live if name not in _prune(lines, retain, prefix)]


def _line(created: str, name: str, removed: str = "false") -> str:
    return f"{created}|{removed}|{name}"


_FIVE = [
    _line("2026-08-17T10:00:00Z", "autodeploy-widget-11111111-widget-config"),
    _line("2026-08-21T10:00:00Z", "autodeploy-widget-55555555-widget-config"),
    _line("2026-08-18T10:00:00Z", "autodeploy-widget-22222222-widget-config"),
    _line("2026-08-20T10:00:00Z", "autodeploy-widget-44444444-widget-config"),
    _line("2026-08-19T10:00:00Z", "autodeploy-widget-33333333-widget-config"),
]

_NEWEST = "autodeploy-widget-55555555-widget-config"


# ----------------------------------------------------------------------- the retention window


def test_the_newest_snapshot_is_never_pruned() -> None:
    """Slice 7b reverts to the most recent snapshot.

    A retention pass that races a rollback destroys the recovery point it exists to protect, so
    this holds at every retain value including the ones a caller should not pass.
    """
    for retain in (0, 1, 2, 3, 5, 99):
        assert _NEWEST not in _prune(_FIVE, retain), (
            f"retain={retain} made the newest snapshot a deletion candidate"
        )


def test_retain_zero_clamps_to_the_newest_rather_than_deleting_everything() -> None:
    assert _kept(_FIVE, 0) == [_NEWEST]
    assert len(_prune(_FIVE, 0)) == 4


def test_it_keeps_the_newest_n_and_prunes_the_rest_oldest_first() -> None:
    assert _kept(_FIVE, 3) == [
        _NEWEST,
        "autodeploy-widget-44444444-widget-config",
        "autodeploy-widget-33333333-widget-config",
    ]
    assert _prune(_FIVE, 3) == [
        "autodeploy-widget-22222222-widget-config",
        "autodeploy-widget-11111111-widget-config",
    ]


def test_a_window_that_is_not_full_prunes_nothing() -> None:
    assert _prune(_FIVE[:2], 3) == []


def test_creation_order_decides_not_listing_order() -> None:
    """kubectl returns items in name order, which for a SHA-tagged name is arbitrary.

    Sorting on the wrong field would keep three arbitrary snapshots and delete the newest often
    enough to look like bad luck rather than a bug.
    """
    assert _prune(list(reversed(_FIVE)), 3) == _prune(_FIVE, 3)


def test_markremoved_snapshots_are_excluded_from_the_window_not_counted_in_it() -> None:
    """A CR that survives its own delete is normal — the finalizer coalesces asynchronously.

    Counting three of those as the retained three would make the next pass delete live
    snapshots to make room, which is the opposite of what retention is for.
    """
    lines = _FIVE + [
        _line(
            "2026-08-22T10:00:00Z", "autodeploy-widget-66666666-widget-config", "true"
        ),
        _line(
            "2026-08-23T10:00:00Z", "autodeploy-widget-77777777-widget-config", "true"
        ),
        _line(
            "2026-08-24T10:00:00Z", "autodeploy-widget-88888888-widget-config", "true"
        ),
    ]
    kept = _kept(lines, 3)
    assert kept == _kept(_FIVE, 3), (
        "a markRemoved CR displaced a live snapshot from the window"
    )
    # And they are not re-deleted: deleting an already-removed snapshot is churn that reads as
    # progress, the failure mode longhorn-reap-orphan-snapshots.sh.j2 documents having shipped.
    assert not [name for name in _prune(lines, 3) if "6666" in name or "7777" in name]


def test_an_unpopulated_markremoved_is_treated_as_not_removed() -> None:
    """R14: a snapshot read moments after creation can have `status.markRemoved` still
    unpopulated, rendering `<ts>||<name>` rather than `<ts>|false|<name>`.

    An `equalto 'false'` filter drops that line out of the listing entirely — including THIS
    run's own snapshot, which fails the "found this run's snapshot" assert and the whole deploy
    before the apply, over a field that just hasn't been written yet. It must be counted as
    live, the same as an explicit 'false'.
    """
    empty_removed_line = (
        "2026-08-21T10:05:00Z||autodeploy-widget-99999999-widget-config"
    )
    lines = _FIVE + [empty_removed_line]
    live = _render(
        _live_expression(),
        volume_snapshot_existing={"stdout_lines": lines},
        volume_snapshot_prefix="autodeploy-widget-",
    )
    assert "autodeploy-widget-99999999-widget-config" in live


def test_snapshots_this_role_did_not_take_are_never_candidates() -> None:
    """Longhorn's own RecurringJob snapshots share the volume and must be left alone.

    Deleting one would silently break the incremental-backup chain the daily and weekly tiers
    diff against.
    """
    lines = _FIVE + [
        _line("2026-08-10T10:00:00Z", "daily-ba-4cd1b236-7e1e-4de1-bd0f-d419ffd6d5ad"),
        _line("2026-08-11T10:00:00Z", "c3f2c932-d89d-46f2-ac2c-34cafbab297e"),
    ]
    assert _prune(lines, 3) == _prune(_FIVE, 3)


def test_an_empty_listing_prunes_nothing_rather_than_erroring() -> None:
    assert _prune([], 3) == []


# ------------------------------------------------------------------ the name/prefix coupling


def test_the_snapshot_name_starts_with_the_prefix_the_prune_selects_on() -> None:
    """The one coupling that makes retention work at all.

    The prune matches `volume_snapshot_prefix`; the create uses `volume_snapshot_name`. Drift
    between them is invisible — every deploy takes a snapshot, no deploy ever prunes one, and
    the volume slowly fills with recovery points that also pin every block beneath them against
    `filesystem trim`.
    """
    facts = _named(_CLAIM, "Name the pre-deploy snapshot")["ansible.builtin.set_fact"]
    context = {
        "volume_snapshot_service": "widget",
        "volume_snapshot_claim": "widget-config",
        "volume_snapshot_sha": {"stdout": "a1b2c3d4\n"},
        "volume_snapshot_pvc": {"stdout": "pvc-0000\n"},
        "volume_snapshot_run_token": "20260821120000",
    }
    name = str(_render(facts["volume_snapshot_name"], **context)).strip()
    prefix = str(_render(facts["volume_snapshot_prefix"], **context))

    assert name.startswith(prefix)
    # The design's `autodeploy-<svc>-<sha8>` survives verbatim as a prefix, because slice 7b
    # reconstructs that string from the service and the deploy tag and matches on it.
    assert name.startswith("autodeploy-widget-a1b2c3d4")
    assert _prune([_line("2026-08-21T10:00:00Z", name)], 0, prefix) == []


def test_the_full_name_has_the_sha_claim_string_as_a_strict_prefix() -> None:
    """R2: the run token makes `autodeploy-<svc>-<sha8>-<claim>` non-unique by design (a
    rollback redeploy must not collide with that commit's earlier snapshot), so 7b's
    reconstruction is a prefix match rather than an equality test. Pin that the reconstructable
    string — service, sha8, and claim, with no run token — is still an exact prefix of whatever
    this role actually names the CR, and that the token is what comes after it.
    """
    facts = _named(_CLAIM, "Name the pre-deploy snapshot")["ansible.builtin.set_fact"]
    context = {
        "volume_snapshot_service": "widget",
        "volume_snapshot_claim": "widget-config",
        "volume_snapshot_sha": {"stdout": "a1b2c3d4\n"},
        "volume_snapshot_run_token": "20260821120000",
    }
    name = str(_render(facts["volume_snapshot_name"], **context)).strip()
    assert name == "autodeploy-widget-a1b2c3d4-widget-config-20260821120000"
    assert name.startswith("autodeploy-widget-a1b2c3d4-widget-config")


def test_two_deploys_of_the_same_sha_get_two_names() -> None:
    """R2: redeploying an older commit is the manual rollback this slice exists to enable, and
    it must not be refused by its own snapshot step colliding with that commit's earlier,
    markRemoved-but-not-gone CR. Two runs with different tokens for the same service/claim/sha
    must produce two distinct names, both sharing the reconstructable prefix.
    """
    expression = _named(_CLAIM, "Name the pre-deploy snapshot")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_name"]
    names = {
        str(
            _render(
                expression,
                volume_snapshot_service="widget",
                volume_snapshot_claim="widget-config",
                volume_snapshot_sha={"stdout": "a1b2c3d4"},
                volume_snapshot_run_token=token,
            )
        ).strip()
        for token in ("20260810090000", "20260821120000")
    }
    assert len(names) == 2
    assert all(
        name.startswith("autodeploy-widget-a1b2c3d4-widget-config") for name in names
    )


def test_two_claims_of_one_service_get_two_names() -> None:
    """pihole has two RWO claims. One name for both would make the second `apply` fight the
    first over `spec.volume` instead of taking a second snapshot."""
    expression = _named(_CLAIM, "Name the pre-deploy snapshot")[
        "ansible.builtin.set_fact"
    ]["volume_snapshot_name"]
    names = {
        str(
            _render(
                expression,
                volume_snapshot_service="pihole",
                volume_snapshot_claim=claim,
                volume_snapshot_sha={"stdout": "a1b2c3d4"},
                volume_snapshot_run_token="20260821120000",
            )
        ).strip()
        for claim in ("pihole-etc", "pihole-dnsmasq")
    }
    assert len(names) == 2


def test_the_run_token_is_computed_once_in_main_not_inside_the_claim_loop() -> None:
    """A token recomputed per claim would drift mid-role: the wait task in claim.yml polls for
    the exact name the apply task in the SAME claim.yml pass created, so two different `now()`
    calls for the same claim would never agree on it. Pin that the fact is set in main.yml
    (which runs once per role, before the per-claim loop) and nowhere in claim.yml (which runs
    once per claim, inside that loop).
    """
    main_task = _named(_MAIN, "Compute a per-run token")["ansible.builtin.set_fact"]
    assert "volume_snapshot_run_token" in main_task
    assert not any(
        "volume_snapshot_run_token" in (task.get("ansible.builtin.set_fact") or {})
        for task in _tasks(_CLAIM)
    ), (
        "the run token must be set once in main.yml, not recomputed inside claim.yml's loop"
    )


def test_the_run_token_uses_now_with_no_gathered_facts() -> None:
    """`now(utc=true, ...)` needs no `ansible_date_time` fact, unlike the alternative. Pinned so
    a future edit can't quietly reintroduce a `gather_facts` dependency this role doesn't have."""
    expression = _named(_MAIN, "Compute a per-run token")["ansible.builtin.set_fact"][
        "volume_snapshot_run_token"
    ]
    assert "now(utc=true" in expression.replace(" ", "").replace("'", "")
    assert "ansible_date_time" not in expression


# ------------------------------------------------------------------------- the detached-volume skip


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
    """The volume-state read only runs once the wait has already failed. An empty read (rc!=0,
    or a jsonpath that returned nothing) must not be read as 'attached' and fall through to
    failing the deploy instead of the named skip."""
    assert _detached("false|false", "") is True


# --------------------------------------------------- the maintenance-mode attach (slice 7b)
#
# 7a skipped a detached volume outright, because Longhorn needs a running engine to snapshot
# it. 7b reuses k8s/longhorn-api and the maintenance-mode attach k8s/volume-revert proved: a
# detached claim gets attached with disableFrontend, snapshotted, and detached again, and only
# an attach that itself fails still falls through to the loud "UNPROTECTED" warning.


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
    """Pairs with the attach on the empty ticket key, same as k8s/volume-revert: sending
    `attachmentID` on the attach alone would make this detach remove nothing while still
    returning 200."""
    body = _named(_CLAIM, "Detach")["ansible.builtin.uri"].get("body", {})
    assert body == {}


def test_every_maintenance_api_call_pins_a_single_status_code() -> None:
    for task in _tasks(_CLAIM):
        uri = task.get("ansible.builtin.uri")
        if uri is None:
            continue
        assert uri["status_code"] == 200, task["name"]
        assert uri["url"].startswith("{{ longhorn_api }}/v1/volumes/"), task["name"]


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
    """INVERTED 2026-08-21 by the task-6 drill. This test previously asserted the OPPOSITE — that
    the retake wait reuses `volume_snapshot_ready` so the pre-7b fail-task and prune guards
    "keep working unmodified against whichever attempt actually ran". That design does not work,
    and this test was pinning the bug in place: **a skipped task still sets the variable it
    registers to**, to a result with no `stdout`. On the attached path the retake is skipped, so
    it erased the first wait's real reading and the role failed every normal deploy of all
    thirteen opted-in services.

    Downstream tasks now read the `volume_snapshot_ready_out` fact, which folds the two waits by
    the path actually taken. `test_skipped_retake_wait_keeps_first_read` in
    test_volume_snapshot_register.py is the behavioural half — it runs the role and would have
    caught what this source-text assertion could not."""
    task = _named(_CLAIM, "Wait for the retaken snapshot to become usable")
    assert task["register"] == "volume_snapshot_retake_ready"


def test_the_downstream_fail_reads_the_folded_readiness_fact() -> None:
    """The other half of the fix: the fail task must read the folded fact rather than either
    wait's register directly, or the clobber comes back the moment someone reorders the file."""
    task = _named(_CLAIM, "Fail on a snapshot that never became usable")
    conditions = " ".join(task["when"])
    assert "volume_snapshot_ready_out" in conditions
    assert "volume_snapshot_ready." not in conditions


def test_the_detach_after_maintenance_runs_whenever_the_attach_succeeded_regardless_of_the_snapshot_outcome() -> (
    None
):
    """A snapshot that never became ready after a successful attach must not leave the volume
    attached — a stale attachment ticket costs the NEXT deploy's own maintenance-mode attach a
    full 180s state wait before it fails, naming the wrong cause. So the detach's `when:` must
    depend on the attach outcome alone, never on `volume_snapshot_ready`."""
    when = _named_when("Detach")
    assert _GUARD in when
    assert "volume_snapshot_maintenance_attached | bool" in when
    assert not any("volume_snapshot_ready" in str(clause) for clause in when)


def test_the_maintenance_detach_wait_is_never_suppressed() -> None:
    """The only proof that a 200 detach actually detached. Suppressing it would let the play
    continue with the volume attached in maintenance mode."""
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
    """Mutated 2026-08-21: swapping 'Retake' and 'Attach' (retaking before attaching) and,
    separately, swapping 'Detach' and 'Wait for the retaken snapshot' both went red under this
    test — see the fix-round report for the transcript. Every per-task guard test in this file
    stayed green under both mutations, which is why a positional test exists at all."""
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
    """7a's warning fired for every plainly-detached volume. 7b tries the maintenance-mode
    attach first, so by the time this task runs the attach has already failed — the message
    must say that, not the old blanket 'no running engine' framing that is no longer why the
    claim is unprotected on a first deploy (that case now takes a snapshot instead, see
    CLAUDE.md)."""
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


def test_the_prune_loop_slices_cleanly_at_the_defaulted_floor_values() -> None:
    """Pins the slice syntax against the values `default([])`/`default(1)` produce for a claim
    skipped as detached, where `volume_snapshot_live`/`volume_snapshot_keep` are never set (the
    'Choose which older snapshots' task that sets them shares this task's guard). Ansible's own
    `default` filter — which only coalesces its own Undefined marker, not a bare Jinja one — is
    not exercised through this harness's plain NativeEnvironment, so this pins the syntax it
    defaults into rather than the coalescing itself."""
    loop_expr = _named(_CLAIM, "Prune snapshots beyond the retention window")["loop"]
    assert _render(loop_expr, volume_snapshot_live=[], volume_snapshot_keep=1) == []


# ------------------------------------------------------------------------------- the plumbing


def test_every_mutating_task_is_guarded() -> None:
    """`test_k8s_dry_run.py` derives this cluster-wide; pinned here too because this role's
    mutations are a `delete` against Longhorn snapshots — the one thing a dry run must never do.
    """
    for task in _tasks(_CLAIM):
        body = str(task)
        if "apply" in body or "delete" in body:
            assert _GUARD in str(task.get("when", "")), (
                f"task {task.get('name')!r} mutates the cluster without the "
                f"k8s_no_mutate guard"
            )


def test_the_prune_is_guarded_as_a_whole_not_only_at_the_delete() -> None:
    """Under a no-mutation run the snapshot was never taken, so a window computed from the live
    list is short by one — and the delete it feeds would remove a real snapshot."""
    for fragment in (
        "List this service's live snapshots",
        "Choose which older snapshots to prune",
        "Prune snapshots beyond the retention window",
    ):
        assert _GUARD in str(_named(_CLAIM, fragment).get("when", "")), fragment


def test_the_delete_never_waits_on_the_finalizer() -> None:
    """A Snapshot CR's `longhorn.io` finalizer makes a default `kubectl delete` block until the
    volume coalesces the data — measured hanging a drill run for twelve minutes on 2026-08-21."""
    argv = _named(_CLAIM, "Prune snapshots beyond the retention window")[
        "ansible.builtin.command"
    ]["argv"]
    assert "--wait=false" in argv
    assert "--ignore-not-found" in argv


def test_every_kubectl_call_uses_argv() -> None:
    """`ansible.builtin.command` shlex-splits a `cmd:` string, so a jsonpath containing a space
    is torn in two — the slice-4 defect that made a whole branch dead code for a round. The
    listing's `{range .items[?(...)]}` is exactly that shape."""
    for path in (_MAIN, _CLAIM):
        for task in _tasks(path):
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            assert "argv" in command, (
                f"{path.name}: task {task.get('name')!r} uses `cmd:`; use argv so no shell-like "
                f"split can tear a jsonpath apart"
            )


def test_the_deploy_tag_uses_chdir_not_git_dash_c() -> None:
    """`git -C` does not override GIT_DIR, and a stray GIT_DIR has already made a check in this
    repo operate on the real repository instead of its fixture."""
    command = _named(_MAIN, "Resolve the deploy tag")["ansible.builtin.command"]
    assert "chdir" in command
    assert "-C" not in command["argv"]
    assert not _named(_MAIN, "Resolve the deploy tag").get("become"), (
        "git run as root refuses a checkout it considers to have dubious ownership"
    )


def test_the_reads_every_later_task_depends_on_survive_check_mode() -> None:
    """A `command` task is skipped under --check by default, and a skipped read does not fail —
    it fails its consumer several tasks later with an undefined attribute. That class cost nine
    roles a fix already."""
    for path, fragment in (
        (_MAIN, "Resolve the deploy tag"),
        (_CLAIM, "Resolve the Longhorn volume backing"),
    ):
        assert _named(path, fragment).get("check_mode") is False, fragment


def test_the_role_declares_an_autodeploy_stance() -> None:
    """Every role under roles/k8s/ must, or `k8s_autodeploy_denylist` refuses to render."""
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["k8s_autodeploy"] is False
    assert defaults["k8s_autodeploy_reason"].strip()


# ------------------------------------------------------------------------ the manifests wiring
#
# This role's whole value is a snapshot taken BEFORE the apply that can destroy what it protects.
# A snapshot moved after the apply would still create a CR, still pass readiness, still prune,
# and every test above would keep passing — "wrong without anyone noticing" is exactly the shape
# Task 1's `test_the_snapshot_name_starts_with_the_prefix_the_prune_selects_on` was written to
# catch for the name/prefix coupling, and this is the same trap for the include's position.


def _manifests_tasks() -> list[dict]:
    return yaml.safe_load(_MANIFESTS.read_text()) or []


def _manifests_index(fragment: str) -> int:
    for i, task in enumerate(_manifests_tasks()):
        if fragment in str(task.get("name", "")):
            return i
    raise AssertionError(f"no task in manifests/tasks/main.yml named {fragment!r}")


def test_the_snapshot_include_runs_before_the_apply() -> None:
    assert _manifests_index("Snapshot the stateful volumes") < _manifests_index(
        "Apply manifests"
    )


def test_the_snapshot_include_is_inert_for_a_role_that_never_opts_in() -> None:
    """`k8s_autodeploy_snapshot_pvcs` is what makes the include a no-op for the ~50 services
    that do not declare it — this is the actual guarantee, not the `grep` that finds zero
    declarations today. Task 3 adding declarations must not be able to remove this gate.

    This is a TEXT MATCH against the `when:` condition, not an execution — it proves the guard
    expression is present, not that Ansible actually skips the include at runtime. The runtime
    guarantee (a faithful toy play: roleA with the var set fires, roleB without it is skipped)
    was verified by executing that play, not by this assertion. Treat this as a regression guard
    against the condition being edited away, not as proof of the behaviour by itself.
    """
    task = _manifests_tasks()[_manifests_index("Snapshot the stateful volumes")]
    when = str(task.get("when", ""))
    assert _GUARD in when
    assert "k8s_autodeploy_snapshot_pvcs | default([])" in when


def test_the_snapshot_include_calls_the_right_role_with_the_right_vars() -> None:
    task = _manifests_tasks()[_manifests_index("Snapshot the stateful volumes")]
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/volume-snapshot"
    call_vars = task["vars"]
    assert call_vars["volume_snapshot_claims"] == "{{ k8s_autodeploy_snapshot_pvcs }}"
    assert "manifests_service" in call_vars["volume_snapshot_service"]


# --------------------------------------------------------------------------------- transport


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_listing_jsonpath_parses() -> None:
    """The synthetic listings above are only worth something if the real command produces that
    shape. Run the role's own argv against the live API server.

    This is the seam test `test_cronjob_gate_decision.py` learned to write the hard way: a
    jsonpath kubectl rejects returns rc=1 and an empty read, every retention test above still
    passes, and the prune silently never deletes anything.

    kubectl's jsonpath has no `&&` — verified 2026-08-21, `unrecognized character in action:
    U+0026` — which is why the volume filter is one comparison and markRemoved is filtered in
    Jinja. This test is what catches someone folding them back together.
    """
    argv = _named(_CLAIM, "List this service's live snapshots")[
        "ansible.builtin.command"
    ]["argv"]
    rendered = [
        str(_render(token, volume_snapshot_volume="pvc-does-not-exist"))
        for token in argv
    ]
    # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
    # kubeconfig, and `k3s kubectl` needs root here.
    assert rendered[0] == "k3s"
    result = subprocess.run(
        rendered[1:], capture_output=True, text=True, timeout=30, check=False
    )
    unreachable_tokens = (
        "connection refused",
        "was refused",
        "i/o timeout",
        "no configuration has been provided",
    )
    if any(token in result.stderr for token in unreachable_tokens):
        pytest.skip("no reachable cluster")
    assert result.returncode == 0, (
        f"kubectl rejected the listing jsonpath: {result.stderr.strip()}"
    )
    # A filter matching nothing returns empty, which is the correct answer for a volume that
    # does not exist — and proves the expression parsed rather than erroring.
    assert result.stdout.strip() == ""


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_listing_fields_exist_on_a_real_snapshot() -> None:
    """The retention decision reads creationTimestamp, markRemoved and name. A field Longhorn
    renames would make every line unparseable and every prune a no-op, with nothing failing."""
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "snapshots.longhorn.io",
            "-o",
            'jsonpath={range .items[*]}{.metadata.creationTimestamp}{"|"}'
            '{.status.markRemoved}{"|"}{.metadata.name}{"\\n"}{end}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("no reachable cluster, or no Snapshot CRs to read")
    for line in result.stdout.strip().splitlines():
        created, removed, name = line.split("|")
        assert created.endswith("Z")
        assert removed in ("true", "false"), (
            f"markRemoved read as {removed!r}; the retention filter treats anything other than "
            f"the literal 'true' as not-removed, so this is unexpected regardless — a renamed "
            f"field reads as empty (not-removed, harmless) but any OTHER unexpected value here "
            f"would silently retain a snapshot that should have dropped out of the window"
        )
        assert name
