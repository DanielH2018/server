"""`files/check_table.py` against the registry, the gates, the env, SOPS and the deploy's reading.

The table is the one place a check is declared (#3659, #3788). The registry and the gate sets
derive from it in Python, and the env-secret and the Kuma tiles derive from it through
`ansible/filter_plugins/py_table.py`, which parses the module instead of running it. What is
left to check is that each derivation reads what the table says.

The Kuma side, that each row's token reaches a push tile, is
`ansible/tests/services/test_kuma_static_monitors.py::test_every_bridge_push_token_reaches_a_push_tile`.
"""

import dataclasses
import inspect
from pathlib import Path

import check
import registry
from bridge.types import PushCheck, push_env
from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import ANSIBLE
from py_table import py_table

from _bridge_env import bridge_env

_TABLE = Path(__file__).resolve().parents[1] / "files" / "check_table.py"
_GATES = frozenset({"prometheus", "b2_reachable", "wan_reachable"})
_GATE_VALUES = _GATES | {"startup_grace", None}


def _unknown_gates(rows) -> list[str]:
    return sorted(
        "%s: %r" % (row.name, row.gate) for row in rows if row.gate not in _GATE_VALUES
    )


def test_each_name_and_kuma_id_appears_once():
    names = [row.name for row in CHECKS]
    ids = [row.kuma_id for row in CHECKS]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_the_registry_runs_every_row_gate_rows_first_in_table_order():
    registered = [c.name for c in registry.build_checks({})]
    # Named members, so an empty table cannot make both sides equal and empty.
    assert {"n8n", "host_packages"} <= set(registered)
    gates = [row.name for row in CHECKS if row.is_gate]
    assert registered == gates + [row.name for row in CHECKS if not row.is_gate]
    assert {c.fn for c in registry.build_checks({})} == {row.fn for row in CHECKS}


def test_the_gate_rows_are_the_three_gates_the_run_loop_skips_by():
    assert {row.name for row in CHECKS if row.is_gate} == _GATES == set(check.GATE_SKIP)


def test_every_registered_check_carries_its_rows_gate():
    assert {c.name: c.gate for c in registry.build_checks({})} == {
        row.name: row.gate for row in CHECKS
    }
    assert {"host_packages"} <= {row.name for row in CHECKS if row.gate == "prometheus"}


def test_a_row_that_names_a_gate_comes_after_that_gate_in_the_registry():
    names = [c.name for c in registry.build_checks({})]
    for c in registry.build_checks({}):
        if c.gate in check.GATE_SKIP:
            assert names.index(c.gate) < names.index(c.name), c.name


def _prometheus_readers(checks) -> set[str]:
    """Registry names, gate rows aside, whose check body names prom_vector or prom_scalar.

    Direct calls only: a check reaching Prometheus through a helper of its own passes vacuously.
    """
    return {
        c.name
        for c in checks
        if c.name not in check.GATE_SKIP
        and any(t in inspect.getsource(c.fn) for t in ("prom_vector", "prom_scalar"))
    }


def _ungated_readers(checks) -> set[str]:
    return {
        c.name
        for c in checks
        if c.name in _prometheus_readers(checks) and c.gate != "prometheus"
    }


def test_every_prometheus_reader_is_gated_on_prometheus():
    """A reader outside the `prometheus` column pages on its own during a Prometheus outage,
    alongside the gate that already reported it."""
    checks = registry.build_checks({})
    # Non-vacuity: an empty derivation would make the assertion below pass on nothing.
    assert {"host_packages"} <= _prometheus_readers(checks)
    assert _ungated_readers(checks) == set()


def test_a_prometheus_reader_that_loses_its_gate_is_flagged():
    checks = [
        dataclasses.replace(c, gate=None) if c.name == "host_packages" else c
        for c in registry.build_checks({})
    ]
    assert _ungated_readers(checks) == {"host_packages"}


def test_every_gate_value_names_a_gate_or_the_startup_grace_is_clean():
    assert _unknown_gates(CHECKS) == []


def test_a_misspelt_gate_value_is_flagged():
    """The red half: a typo'd gate would leave its check suppressed by nothing."""
    typo = dataclasses.replace(CHECKS[0], gate="promethues")
    assert _unknown_gates([typo]) == ["%s: 'promethues'" % CHECKS[0].name]


def test_the_deploy_reads_every_field_but_fn_off_each_row():
    """`py_table` keeps only literal arguments, so a computed value would vanish from the deploy.

    A description built from a constant would render a tile with no description, and AutoKuma
    would rewrite the live monitor to match. Comparing the parsed table to the imported one
    catches that before a render does.
    """
    parsed = py_table(_TABLE.read_text(), "CHECKS")
    defaults = {
        f.name: f.default
        for f in dataclasses.fields(PushCheck)
        if f.default is not dataclasses.MISSING
    }
    assert len(parsed) == len(CHECKS) >= 20
    for row, read in zip(CHECKS, parsed, strict=True):
        expected = dataclasses.asdict(row)
        del expected["fn"]
        assert {**defaults, **read} == expected, row.name


def test_the_env_secret_renders_one_token_per_row_under_the_name_the_bridge_reads():
    rendered = {key for key in bridge_env() if key.startswith("KUMA_PUSH_")}
    assert (
        "KUMA_PUSH_HA_HEARTBEAT" in rendered
    )  # a row whose secret predates the naming rule
    assert rendered == {push_env(row.name) for row in CHECKS}


def test_every_row_names_a_secret_sops_declares():
    # SOPS encrypts values, not keys, so the key list is readable without the age key. A row
    # whose secret is missing fails the deploy's render at lookup('vars').
    declared = set(yaml_fast.safe_load((ANSIBLE / "vars/secrets.yml").read_text()))
    missing = sorted(row.token for row in CHECKS if row.token not in declared)
    assert not missing, missing
