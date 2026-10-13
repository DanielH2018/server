"""Guards the live exempt-set gates the netpol-baseline role runs under namespace scope.

Split from `test_netpol_baseline_labels.py`, which guards the rendered labels; this file reads
the role's own tasks and defaults instead.

Run: uv run pytest ansible/tests/k8s/test_netpol_baseline_exempt_gates.py
"""

from _helpers import K8S_ROLES

# Both baselines are namespace-scoped, i.e. opt-out. Under opt-out the
# exempt set IS the boundary, so who carries the label stops being bookkeeping and becomes the
# fence itself. Only `homelab` got a cluster-side reconcile; the observability baseline ran opt-out
# with nothing checking who had opted out.
#
# Every guard in test_netpol_baseline_labels.py is a TEMPLATE-side guard, and template-side
# guards are precisely what stay green when the drift is in the cluster. So this file asserts
# the existence of the runtime gate rather than duplicating its logic.

_ROLE = K8S_ROLES / "netpol-baseline"
TASKS = (_ROLE / "tasks" / "main.yml").read_text()


def test_each_namespace_scope_lever_has_a_live_exempt_gate():
    """A namespace-scoped baseline without a live gate is an unenforced invariant."""
    for scope_var, ns_var in (
        ("netpol_baseline_scope", "k8s_namespace"),
        ("netpol_baseline_obs_scope", "k8s_observability_namespace"),
    ):
        assert f"{scope_var} == 'namespace'" in TASKS, (
            f"{scope_var} has no live exempt-set gate — under namespace scope the exempt "
            "label is the only way out of the fence, so nothing reconciles it against the cluster"
        )
        assert f"-n {{{{ {ns_var} }}}} get pods -l netpol-baseline-exempt" in TASKS, (
            f"the gate for {scope_var} must read {ns_var}, not another namespace — "
            "homelab's gate was scoped to k8s_namespace and silently covered nothing else"
        )


def test_observability_expects_an_empty_exempt_set():
    """An exemption here leaves a pod fenced by nothing, or by only its homelab-caller grant.

    The second is prometheus, loki and alertmanager; tasks/main.yml has why that breaks them."""
    defaults = (_ROLE / "defaults" / "main.yml").read_text()
    assert "netpol_baseline_obs_exempt_workloads: []" in defaults, (
        "observability's expected exempt set must be empty and explicit — an exemption there "
        "leaves a pod fenced by nothing, or by a caller grant that cuts its in-namespace callers"
    )
