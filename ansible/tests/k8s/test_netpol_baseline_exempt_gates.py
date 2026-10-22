"""Guards the observability baseline's expected exempt set.

Split from `test_netpol_baseline_labels.py`, which guards the rendered labels; this file reads
the role's defaults instead. The live exempt-set gates are the `exempt` check of the
`netpol-baseline-verify` Job's `gate` initContainer, which
`ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_flux_gates.py` pins.

Run: uv run pytest ansible/tests/k8s/test_netpol_baseline_exempt_gates.py
"""

from _helpers import K8S_ROLES

_ROLE = K8S_ROLES / "netpol-baseline"


def test_observability_expects_an_empty_exempt_set():
    """An exemption here leaves a pod fenced by nothing, or by only its homelab-caller grant.

    That second case is prometheus, loki and alertmanager, which each lose their in-namespace
    callers once exempt (the defaults file's comment on the variable has why)."""
    defaults = (_ROLE / "defaults" / "main.yml").read_text()
    assert "netpol_baseline_obs_exempt_workloads: []" in defaults, (
        "observability's expected exempt set must be empty and explicit — an exemption there "
        "leaves a pod fenced by nothing, or by a caller grant that cuts its in-namespace callers"
    )
