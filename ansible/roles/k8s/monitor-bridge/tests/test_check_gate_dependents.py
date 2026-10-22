"""The gate membership sets: every name in one must be a real check.

A typo on either side of a gate's dependent set stops guarding that gate and changes nothing
else — the filter would then accept exactly the configuration the gate exists to prevent, and a
single outage would page across every dependent. These guards are what keeps the five sets and
`GATE_DEPENDENTS` pinned to the registry as checks are renamed.

The suppression BEHAVIOUR each set drives lives in `test_check_gates.py`; this file is only
about membership.
"""

import inspect

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
        "wan_reachable",
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


def test_cluster_targets_is_prom_dependent():
    # It reads the one Prometheus, so the `prometheus` gate watches its source. Its own floor
    # stays the arm for "Prometheus answers but the targets went away", which the gate cannot
    # see — the reason it is gated and not merely floored.
    assert "cluster_targets" in gates.PROM_DEPENDENT


# ── The COMPLETENESS axis: a Prometheus reader missing from every gate set ──────────────────
# The guards above assert `<SET> <= names` — every member is a real check. That direction
# cannot see a check that reads Prometheus and is in NO set: it co-fires with the
# `prometheus` gate, one root cause paging twice.


def prom_readers(checks) -> set[str]:
    """Registry names whose check body names prom_vector or prom_scalar in its source.

    Derived from the registry rather than transcribed, so a renamed check follows. Direct
    calls only: a check that reaches Prometheus through a helper of its own passes this
    vacuously, and chasing transitive calls would trade a guard that is exactly right about
    what it sees for one that is approximately right about everything.
    """
    return {
        c.name
        for c in checks
        if any(
            token in inspect.getsource(c.fn) for token in ("prom_vector", "prom_scalar")
        )
    }


def test_prom_readers_are_derivable_from_the_registry():
    """Non-vacuity: an empty derivation makes the completeness guard below pass on nothing."""
    readers = prom_readers(registry.build_checks())
    assert {"disk", "targets", "traefik_latency"} <= readers, (
        f"prom readers derived from the registry look wrong: {sorted(readers)} — "
        "the source-scan or the registry shape changed, and the guard below is now inert"
    )


def test_every_prometheus_reader_is_gated():
    """Every check reading Prometheus is suppressed by the gate that watches its instance.

    One Prometheus remains, so PROM_DEPENDENT is the whole membership. A reader outside it
    pages on its own during a Prometheus outage, alongside the gate that already reported it.
    """
    ungated = prom_readers(registry.build_checks()) - gates.PROM_DEPENDENT
    assert not ungated, (
        f"check(s) {sorted(ungated)} query Prometheus but are not in PROM_DEPENDENT, so a "
        "Prometheus outage pages them alongside the gate. Add each to PROM_DEPENDENT."
    )


def test_a_reader_dropped_from_the_gate_sets_is_flagged():
    """The reject half: a Prometheus reader in no gate set must not read as clean."""
    readers = prom_readers(registry.build_checks())
    without_latency = gates.PROM_DEPENDENT - {"traefik_latency"}
    assert readers - without_latency == {"traefik_latency"}
