"""netpol-baseline's probe target table: the rows themselves must be well formed.

The verify Job and its `gate` initContainer both read `netpol_baseline_probe_active`, so a row is
gated and dialled from one list. The Job's rendering of that list, and the lever that picks each
plane's rows, are pinned by
`ansible/roles/k8s/netpol-baseline/tests/test_netpol_baseline_verify.py`.
"""

from lib import yaml_fast
from _helpers import ALL_VARS, K8S_ROLES

ROLE = K8S_ROLES / "netpol-baseline"
DEFAULTS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
TARGETS = DEFAULTS["netpol_baseline_probe_targets"]


def test_every_plane_names_a_namespace() -> None:
    """The gate reads a row's EndpointSlice in `netpol_baseline_probe_namespaces[plane]`, so a
    plane missing from the map fails the Job at the gate rather than in a render."""
    planes = {r["plane"] for r in TARGETS}
    assert planes == {"homelab", "observability", "flux-system"}
    assert planes <= set(DEFAULTS["netpol_baseline_probe_namespaces"])


def test_terraria_is_absent_while_it_is_scaled_to_zero() -> None:
    """terraria runs at replicas 0, so gating it on a ready endpoint can only fail.

    One direction only. A count above 0 does not require the row: its `expect: open` leg dials
    :7777, which can crash a 1.4.5.7 server (#822), so the row comes back with the image fix
    rather than with the replica count. defaults/main.yml records the decision (#3848).
    """
    # In inventory, not terraria's defaults — k8s/game-stats renders the exporter
    # at the same count and a role default does not cross a role boundary.
    all_vars = yaml_fast.safe_load(ALL_VARS.read_text())
    services = {r["service"] for r in TARGETS}
    if int(all_vars["terraria_k8s_replicas"]) == 0:
        assert "terraria" not in services, (
            "terraria is scaled to zero but is still a probe target, so every revision's "
            "verify Job fails its readiness gate"
        )


def _refused_rows_without_policy(rows: list[dict]) -> set[str]:
    return {
        r["service"]
        for r in rows
        if r.get("expect") == "refused" and not r.get("policy")
    }


def test_every_refused_row_names_its_policy() -> None:
    """A refused leg's failure message prints the row's `policy`, the fence that let the
    connection through. A row without the key fails the Job with a message naming none, and
    only on netpol-baseline's real revision."""
    assert not _refused_rows_without_policy(TARGETS), (
        f"refused rows with no `policy`: {sorted(_refused_rows_without_policy(TARGETS))}. "
        "Name the NetworkPolicy template or netpol_from entry that should refuse the probe."
    )
    # The check above can go red: a refused row without the key is found.
    unnamed = {
        "service": "sonarr-two",
        "plane": "homelab",
        "port": 1,
        "expect": "refused",
    }
    assert _refused_rows_without_policy([*TARGETS, unnamed]) == {"sonarr-two"}
