"""The gate membership sets: every name in one must be a real check.

A typo on either side of a gate's dependent set stops guarding that gate and changes nothing
else — the filter would then accept exactly the configuration the gate exists to prevent, and a
single outage would page across every dependent. These guards are what keeps the five sets and
`GATE_DEPENDENTS` pinned to the registry as checks are renamed.

The suppression BEHAVIOUR each set drives lives in `test_check_gates.py`; this file is only
about membership.
"""

import gates
import registry


def test_prom_dependent_set_matches_real_checks():
    # Guard: every name in PROM_DEPENDENT is a real check, so the gate can't silently drift.
    names = {c.name for c in registry.build_checks()}
    assert gates.PROM_DEPENDENT <= names


def test_loki_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT): every name in LOKI_DEPENDENT is a real check.
    names = {c.name for c in registry.build_checks()}
    assert gates.LOKI_DEPENDENT <= names


def test_b2_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT): every name in B2_DEPENDENT is a real check.
    names = {c.name for c in registry.build_checks()}
    assert gates.B2_DEPENDENT <= names


# `test_b2_dependent_excludes_backup` was deleted here on 2026-09-01. It asserted `"backup" not
# in B2_DEPENDENT` to keep the B2 gate from suppressing the one check that polled B2's real
# state — sound while it was written, dead since Kopia retired on 2026-08-13 (ADR-0014) and backup
# moved to Longhorn. No check named `backup` exists in the registry or STARTUP_GRACE any more, so
# the assertion could not fail, and its comment described two behaviours that had stopped being
# true. Both invariants it pointed at are still enforced, by name: `B2_DEPENDENT <= names`
# directly above, and the STARTUP_GRACE disjointness in
# test_check_streaks.py::test_startup_grace_disjoint_from_run_once_skip_sets.


def test_cluster_dependent_set_matches_real_checks():
    # Guard (mirrors PROM_DEPENDENT/LOKI_DEPENDENT/B2_DEPENDENT): every name is a real check.
    names = {c.name for c in registry.build_checks()}
    assert gates.CLUSTER_DEPENDENT <= names


def _dependents_are_real_checks(dependents_map: dict, names: set) -> bool:
    """True when every check named across `dependents_map`'s values is a real registry name.

    Shared by the real guard and its rejecting half below, so a helper weakened to always
    return True fails the rejecting half rather than passing both silently.
    """
    return set().union(*dependents_map.values()) <= names


def test_gate_dependents_maps_real_gates_to_real_checks():
    """Guard (mirrors the four *_DEPENDENT sets above): both halves of GATE_DEPENDENTS are real.

    validate_check_filter reads this map to refuse a CHECKS_ONLY/CHECKS_SKIP filter that turns a
    gate off while leaving its dependents on. A typo on either side stops guarding one gate and
    changes nothing else, so the filter would accept exactly the configuration the gate exists to
    prevent — silently, since the four sets it is built from each have their own guard and would
    still pass.
    """
    names = {c.name for c in registry.build_checks()}
    assert _dependents_are_real_checks(gates.GATE_DEPENDENTS, names)
    # The KEYS are deliberately NOT check names. The four reachability gates are evaluated by
    # run_once directly and have no registry entry, which is why validate_check_filter unions them
    # into `known` separately — so they are pinned against the gates run_once actually evaluates.
    assert set(gates.GATE_DEPENDENTS) == {
        "prometheus",
        "loki_reachable",
        "b2_reachable",
        "cluster_prometheus",
    }
    assert set(gates.GATE_DEPENDENTS).isdisjoint(names)


def test_a_gate_dependent_typo_would_be_caught():
    """The rejecting half: the assertion above must go red on a dependent that is not a check."""
    names = {c.name for c in registry.build_checks()}
    typo = {"prometheus": frozenset({"disk", "disk_typoo"})}
    assert not _dependents_are_real_checks(typo, names)
    assert not set(typo) == set(gates.GATE_DEPENDENTS)


def test_exporter_dependent_union_is_real_checks_and_not_empty():
    """Guard for EXPORTER_DEPENDENT as a whole, beside its per-key sibling in the exporters suite.

    The non-vacuity half is the load-bearing one: `set().union(*{}.values())` is empty and is a
    subset of everything, so an EXPORTER_DEPENDENT emptied by a bad edit would satisfy the subset
    assertion while suppressing nothing.
    """
    names = {c.name for c in registry.build_checks()}
    dependents = set().union(*gates.EXPORTER_DEPENDENT.values())
    assert dependents <= names
    assert {"disk", "memory", "host_temp"} <= dependents


def test_cluster_dependent_disjoint_from_prom_dependent():
    # The whole point of a second gate: k8s_workloads reads the CLUSTER Prometheus, so it must not
    # also be suppressed by the DOCKER Prometheus gate. Being in both would mean a Docker-side
    # outage silences a check whose source is fine, and vice versa.
    assert gates.CLUSTER_DEPENDENT.isdisjoint(gates.PROM_DEPENDENT)
    assert gates.CLUSTER_DEPENDENT.isdisjoint(gates.LOKI_DEPENDENT)
    assert gates.CLUSTER_DEPENDENT.isdisjoint(gates.B2_DEPENDENT)


def test_a_gates_value_validates_against_its_own_dependent_sets():
    """`Gates.gate_dependents()` is what the filter is validated against, not the module table.

    `run_once` suppresses by the four sets on the VALUE it is handed, so a filter validated
    against `GATE_DEPENDENTS` instead would accept a configuration the run loop then treats
    differently. A `Gates` whose Prometheus gate suppresses nothing must therefore leave
    `--check disk` alone, where the module table unions `prometheus` in.
    """
    stated = gates.Gates(prom_dependent=frozenset())
    assert stated.gate_dependents()["prometheus"] == frozenset()
    names = frozenset({"disk"})
    assert gates.expand_gates_for_cli(names, stated.gate_dependents()) == names
    assert "prometheus" in gates.expand_gates_for_cli(
        names
    )  # the module table still does


def test_cluster_targets_is_cluster_dependent_not_prom_dependent():
    # It reads the CLUSTER Prometheus, so a Docker-side outage must not suppress it and vice
    # versa — the same separation k8s_workloads has.
    assert "cluster_targets" in gates.CLUSTER_DEPENDENT
    assert "cluster_targets" not in gates.PROM_DEPENDENT
