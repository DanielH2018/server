"""Every rendered Deployment's `progressDeadlineSeconds` covers its role's rollout budget.

WHY THIS EXISTS. k8s/rollout-drain runs `kubectl rollout status --timeout=<budget>`, with the
budget taken from the role's `manifests_rollout_timeout`. `rollout status` also exits non-zero
as soon as the Deployment is marked `ProgressDeadlineExceeded`, which Kubernetes does after
`progressDeadlineSeconds` (default 600) without progress. A pod stuck `Pulling` makes no
progress, so a budget above the deadline is unreachable. valheim measured that on 2026-08-13.
prowlarr then raised its budget to 780s for a flaresolverr cold pull that took 8m54s (#2369),
and that budget holds only because both of its templates raise the deadline to match.

Run: uv run pytest ansible/tests/k8s/test_progress_deadline_covers_rollout_timeout.py
"""

from _helpers import REPO, manifests_rollout_timeout_s
from _k8s_render import rendered_docs

_K8S_ROLES = REPO / "ansible/roles/k8s"
_DEFAULT_DEADLINE_S = 600

# Budgets above the default deadline with no matching deadline yet. #2370 tracks the fix; drop
# a role from here when its template renders the deadline.
_KNOWN_SHORT = frozenset({"sonarr", "radarr"})

# Non-vacuity: the Deployments whose budget exceeds the default and that DO cover it. If the
# census stops finding them, the check below passes over nothing.
_MUST_COVER = frozenset({"prowlarr", "flaresolverr", "valheim"})


def deadline_offence(spec: dict, budget_s: int) -> str | None:
    """None when the spec's progress deadline is at least `budget_s`, a reason otherwise."""
    deadline = spec.get("progressDeadlineSeconds", _DEFAULT_DEADLINE_S)
    if deadline >= budget_s:
        return None
    return (
        f"progressDeadlineSeconds {deadline} is below the {budget_s}s rollout budget, so "
        "`rollout status` fails on ProgressDeadlineExceeded before its own timeout"
    )


def test_deadline_offence_is_clean_when_the_deadline_matches_the_budget():
    assert deadline_offence({"progressDeadlineSeconds": 1200}, 1200) is None
    assert deadline_offence({}, 300) is None


def test_deadline_offence_is_flagged_when_the_default_deadline_is_below_the_budget():
    assert deadline_offence({}, 1200) is not None
    assert deadline_offence({"progressDeadlineSeconds": 900}, 1200) is not None


def test_every_deployment_deadline_covers_its_rollout_budget():
    offenders, covered = [], set()
    for role, tpl, doc in rendered_docs():
        if doc.get("kind") != "Deployment" or role in _KNOWN_SHORT:
            continue
        budget = manifests_rollout_timeout_s(_K8S_ROLES / role)
        name = doc["metadata"]["name"]
        if reason := deadline_offence(doc.get("spec", {}), budget):
            offenders.append(f"{role}/{tpl} ({name}): {reason}")
        elif budget > _DEFAULT_DEADLINE_S:
            covered.add(name)
    assert _MUST_COVER <= covered, (
        f"never checked {sorted(_MUST_COVER - covered)} — the census or the render broke"
    )
    assert not offenders, "\n".join(offenders)
