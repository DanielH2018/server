"""Every rendered Deployment pins `revisionHistoryLimit: 3`.

WHY THIS EXISTS. Kubernetes defaults `revisionHistoryLimit` to 10, so an unpinned Deployment
keeps ten scaled-to-zero ReplicaSets as rollback history. Across this cluster that read as 605
ReplicaSets against 68 Deployments and 119 pods on 2026-08-28 — 537 of them empty. Nothing
breaks, but `kubectl get rs -A` stops being usable for reading cluster state, and the count
grows with every image-pin bump the GitOps tick lands.

Rollback depth is not the reason to raise the number back. Rollbacks here go through
`git revert` + a redeploy, not `kubectl rollout undo`, so history beyond the handful needed to
read a recent rollout is not load-bearing.

The pin is emitted by `spec_shell` in `ansible/templates/workload-shell.yml.j2`, and
`test_workload_shell_uses_the_macros.py` refuses a Deployment template that writes it by hand.
What is left for this file is the half a grep cannot do: a template that never calls the
macro omits the field and still renders, so the census reads the RENDERED spec. A DaemonSet
has the field too but owns no ReplicaSets, so it is out of scope and left to its author.

Run: uv run pytest ansible/tests/k8s/test_deployment_revision_history_limit.py
"""

from _k8s_render import rendered_docs

LIMIT = 3

# Non-vacuity: 62 Deployments render today (2026-09-19). Close enough to notice a contraction.
_MIN_DEPLOYMENTS = 55


def limit_offence(spec: dict) -> str | None:
    """None for a spec pinned to LIMIT, a reason otherwise."""
    value = spec.get("revisionHistoryLimit")
    if value == LIMIT:
        return None
    if value is None:
        return "no revisionHistoryLimit — keeps 10 dead ReplicaSets instead of 3"
    return f"revisionHistoryLimit is {value!r}, not {LIMIT}"


def test_limit_offence_is_clean_on_the_pin():
    assert limit_offence({"revisionHistoryLimit": LIMIT}) is None


def test_limit_offence_is_flagged_on_an_omitted_or_moved_pin():
    assert limit_offence({}) is not None
    assert limit_offence({"revisionHistoryLimit": 10}) is not None


def test_every_deployment_pins_its_revision_history_limit():
    offenders, count = [], 0
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") != "Deployment":
            continue
        count += 1
        if reason := limit_offence(doc.get("spec", {})):
            offenders.append(f"{role}/{tpl} ({doc['metadata']['name']}): {reason}")
    assert count >= _MIN_DEPLOYMENTS, (
        f"only {count} rendered Deployments — coverage shrank, or the render broke"
    )
    assert not offenders, "\n".join(offenders)
