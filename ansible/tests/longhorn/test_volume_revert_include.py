"""How `roles/k8s/manifests` includes `k8s/volume-revert`, and when that include fires.

The revert runs after the snapshot and before the apply, fires only on a real rollback call
(`k8s_restore_snapshot_sha` set, claims declared, mutation allowed), passes the role its own interface,
and never swallows a failed claim. Every role that takes snapshots must also restore its own
replicas, or a revert leaves the workload at zero.
"""

from __future__ import annotations

import yaml
from _k8s_render import rendered_docs
from _helpers import render_expr as _render
from _volume_revert import _MANIFESTS, _REPO, _index, _named, _task_names


def _snapshot_roles() -> dict[str, list[str]]:
    """Every role that opts into a pre-deploy snapshot, and the claims it declares."""
    roles = {}
    for defaults in sorted((_REPO / "ansible/roles/k8s").glob("*/defaults/main.yml")):
        declared = (yaml.safe_load(defaults.read_text()) or {}).get(
            "k8s_autodeploy_snapshot_pvcs"
        )
        if declared:
            roles[defaults.parents[1].name] = declared
    return roles


def test_every_snapshot_role_restores_its_own_replicas() -> None:
    """`volume-revert` scales to zero and never scales back.

    That is only correct because the apply which follows restores the workload, and it restores it
    only if the role renders a Deployment **named for the service** with an **explicit replica
    count**.

    Nothing else in the repo enforces that. A role that opts into a snapshot but runs a StatefulSet,
    names its workload something other than the service, or omits `replicas:` and inherits the API
    server's default would leave its service at zero replicas after a rollback — the outage the
    rollback was meant to end. This is the executable version of a sentence that otherwise lives
    only in a comment.
    """
    roles = _snapshot_roles()
    # A floor, and only as a sanity check on the reader itself: the guard below is the loop,
    # which covers however many roles opt in. Thirteen opted in as of 2026-08-21.
    assert len(roles) >= 13, (
        f"only found {len(roles)} snapshot roles; the reader is broken"
    )

    deployments: dict[str, list[dict]] = {}
    for role, _tpl, doc in rendered_docs():
        if role in roles and doc.get("kind") == "Deployment":
            deployments.setdefault(role, []).append(doc)

    for role in sorted(roles):
        named = [
            doc
            for doc in deployments.get(role, [])
            if doc.get("metadata", {}).get("name") == role
        ]
        assert named, (
            f"{role} declares k8s_autodeploy_snapshot_pvcs but renders no Deployment named "
            f"{role!r}. k8s/volume-revert scales `deployment/{role}` to zero and does not scale "
            f"it back, so this service would stay down after a rollback."
        )
        for doc in named:
            replicas = doc.get("spec", {}).get("replicas")
            assert isinstance(replicas, int) and replicas >= 1, (
                f"{role}'s Deployment has replicas={replicas!r}. It must be an explicit count "
                f"of at least one: the apply after a revert is what brings the workload back."
            )


def test_the_revert_runs_after_the_snapshot_and_before_the_apply() -> None:
    """Order is the whole contract.

    A revert AFTER the apply restores the old data and then the new pod migrates it again. A revert
    BEFORE the snapshot discards the recovery point for the state being replaced. The three-way
    chain pins both adjacent pairs the insertion touches, so moving the revert either direction
    fails one of the two comparisons.
    """
    names = _task_names(_MANIFESTS)
    assert (
        _index(names, "Snapshot the stateful volumes")
        < _index(names, "Revert the stateful volumes")
        < _index(names, "Apply manifests")
    )


def _revert_when() -> list[str]:
    return _named(_MANIFESTS, "Revert the stateful volumes")["when"]


def _revert_fires(**context) -> bool:
    """Whether the revert include's `when:` clauses ALL evaluate true for this context — the
    condition under which `ansible.builtin.include_role` actually runs.

    Renders each clause exactly as `_input_check` renders the role's own input-check clauses
    above, and for the same reason: a substring or regex match on the clause TEXT pins spelling,
    not behaviour, and a spelling pin fails a strictly more correct rewrite of the same clause —
    `| trim | length > 0` is a stricter, CORRECT version of the sha check and a text-anchored
    assert rejects it. Rendering the clause is the only check that stays green under a harmless
    rewrite and red under a behavioural regression.

    This still renders one clause set against one synthetic context — it is not a playbook run.
    The property that matters at runtime is that ~50 services share ONE `k8s_restore_snapshot_sha`
    extra-var across a batch loop and each service's own `k8s_autodeploy_snapshot_pvcs` decides
    whether IT reverts, with no leakage between iterations. That was verified by executing a toy
    play — every service in the loop, three task orderings, on the pinned ansible-core — not by
    this function or the tests that call it. Treat the tests below as a regression guard on the
    condition, not as proof of the batch-loop behaviour by itself.
    """
    return all(
        bool(_render("{{ " + clause + " }}", **context)) for clause in _revert_when()
    )


# k8s_no_mutate=False and both other vars populated: the context under which every clause
# should independently need to hold for the include to fire. Each test below starts here and
# breaks exactly one input.
_FIRES_CONTEXT = {
    "k8s_no_mutate": False,
    "k8s_restore_snapshot_sha": "abc12345",
    "k8s_autodeploy_snapshot_pvcs": ["speedtest-config"],
}


def test_the_revert_fires_on_a_real_rollback_call() -> None:
    """The rejections below prove nothing if the clauses reject a real call too."""
    assert _revert_fires(**_FIRES_CONTEXT)


def test_the_revert_is_inert_without_the_restore_var() -> None:
    """~50 roles call k8s/manifests.

    On every ordinary deploy — which is all of them but a failed auto-deploy's second attempt — this
    include must not run.

    Checked by rendering the clause, not by matching its text: a text-anchored assert pins spelling,
    and a prior version of this test rejected `| trim | length > 0` — a strictly more correct
    rewrite of the same clause (see the whitespace case below) — for failing to match the exact
    punctuation it was written against. `k8s_no_mutate` is held False and the claims list populated
    throughout, so only the sha input varies.
    """
    # never set — the ordinary-deploy case; gitops-deploy sets this extra-var only on a
    # rollback redeploy
    without_sha = {
        k: v for k, v in _FIRES_CONTEXT.items() if k != "k8s_restore_snapshot_sha"
    }
    assert not _revert_fires(**without_sha)
    assert not _revert_fires(**{**_FIRES_CONTEXT, "k8s_restore_snapshot_sha": ""})
    # whitespace-only: `default('')` passes it through unchanged, and a bare length check counts
    # whitespace as content the role would then try to use as a snapshot-prefix SHA.
    assert not _revert_fires(**{**_FIRES_CONTEXT, "k8s_restore_snapshot_sha": "   "})


def test_a_role_with_no_declared_claims_never_reverts() -> None:
    """The extra-var is global to the playbook run, so it is set for every service in the
    failing batch — including stateless ones with no snapshots at all. Those must skip, not
    fail looking for a snapshot that was never taken.

    Checked by rendering the clause, for the same reason as the sha test above: a text-anchored
    assert rejected the redundant outer parens being dropped from this clause, a behaviourally
    identical edit. `k8s_no_mutate` is held False and the sha valid throughout, so only the
    claims input varies.
    """
    without_claims = {
        k: v for k, v in _FIRES_CONTEXT.items() if k != "k8s_autodeploy_snapshot_pvcs"
    }
    assert not _revert_fires(**without_claims)
    assert not _revert_fires(**{**_FIRES_CONTEXT, "k8s_autodeploy_snapshot_pvcs": []})


def test_the_revert_is_inert_under_k8s_no_mutate() -> None:
    """A dry run or `--check` run must not enter the role, whatever the other two inputs say —
    belt and braces alongside the role's own internal guard. Sha and claims are held valid
    throughout, so only `k8s_no_mutate` varies."""
    assert not _revert_fires(**{**_FIRES_CONTEXT, "k8s_no_mutate": True})


def test_the_revert_include_passes_the_roles_own_interface() -> None:
    """The three vars k8s/volume-revert's own assert task requires, named exactly as that role
    reads them — a typo here reads as a missing var at runtime, after the workload is already
    scaled to zero on a real incident."""
    task = _named(_MANIFESTS, "Revert the stateful volumes")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/volume-revert"
    call_vars = task["vars"]
    assert call_vars["volume_revert_claims"] == "{{ k8s_autodeploy_snapshot_pvcs }}"
    assert call_vars["volume_revert_service"] == "{{ manifests_service }}"
    assert call_vars["volume_revert_sha"] == "{{ k8s_restore_snapshot_sha }}"


def test_the_revert_does_not_swallow_a_failed_claim() -> None:
    """The call site must not blunt the role's own fail-fast behaviour.

    `k8s/volume-revert` already stops the play on a failed claim; adding `ignore_errors` or
    `failed_when: false` here would turn that into a partial revert nothing then flags.
    """
    task = _named(_MANIFESTS, "Revert the stateful volumes")
    assert "ignore_errors" not in task
    assert "failed_when" not in task
