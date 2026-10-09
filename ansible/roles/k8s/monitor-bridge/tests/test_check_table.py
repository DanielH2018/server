"""`files/check_table.py` against the registry, the gates, the env, SOPS and the deploy's reading.

The table is the one place a check is declared (#3659, #3788). The registry and the gate sets
derive from it in Python, and the env-secret and the Kuma tiles derive from it through
`ansible/filter_plugins/py_table.py`, which parses the module instead of running it. What is
left to check is that each derivation reads what the table says.

The Kuma side, that each row's token reaches a push tile, is
`ansible/tests/services/test_kuma_static_monitors.py::test_every_bridge_push_token_reaches_a_push_tile`.
"""

import dataclasses
from pathlib import Path

import gates
import registry
from bridge.types import PushCheck, push_env
from check_table import CHECKS
from lib import yaml_fast
from lib.repo_paths import ANSIBLE
from py_table import py_table

from _bridge_env import bridge_env

_TABLE = Path(__file__).resolve().parents[1] / "files" / "check_table.py"
_GATES = frozenset({"prometheus", "loki_reachable", "b2_reachable", "wan_reachable"})
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


def test_the_registry_runs_every_row_but_the_gates_in_table_order():
    registered = [c.name for c in registry.build_checks({})]
    # Named members, so an empty table cannot make both sides equal and empty.
    assert {"disk", "traefik_421", "kubelet_plugin_readonly"} <= set(registered)
    assert registered == [row.name for row in CHECKS if not row.is_gate]
    assert {c.fn for c in registry.build_checks({})} == {
        row.fn for row in CHECKS if not row.is_gate
    }


def test_the_gate_rows_are_the_four_gates_with_the_probes_run_once_uses():
    rows = {row.name: row.fn for row in CHECKS if row.is_gate}
    assert set(rows) == _GATES == set(gates.GATE_DEPENDENTS)
    probes = gates.Gates()
    assert rows == {
        "prometheus": probes.probe_prometheus,
        "loki_reachable": probes.probe_loki,
        "b2_reachable": probes.probe_b2,
        "wan_reachable": probes.probe_wan,
    }


def test_every_gate_set_is_its_column():
    assert {"disk", "traefik_421", "etcd_db_size"} <= gates.PROM_DEPENDENT
    assert gates.STARTUP_GRACE == {
        row.name for row in CHECKS if row.gate == "startup_grace"
    }
    for gate, members in gates.GATE_DEPENDENTS.items():
        assert members == {row.name for row in CHECKS if row.gate == gate}, gate


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
    assert len(parsed) == len(CHECKS) >= 44
    for row, read in zip(CHECKS, parsed, strict=True):
        expected = dataclasses.asdict(row)
        del expected["fn"]
        assert {**defaults, **read} == expected, row.name


def test_the_env_secret_renders_one_token_per_row_under_the_name_the_bridge_reads():
    rendered = {key for key in bridge_env() if key.startswith("KUMA_PUSH_")}
    assert "KUMA_PUSH_MEMORY" in rendered  # a row whose secret predates the naming rule
    assert rendered == {push_env(row.name) for row in CHECKS}


def test_every_row_names_a_secret_sops_declares():
    # SOPS encrypts values, not keys, so the key list is readable without the age key. A row
    # whose secret is missing fails the deploy's render at lookup('vars').
    declared = set(yaml_fast.safe_load((ANSIBLE / "vars/secrets.yml").read_text()))
    missing = sorted(row.token for row in CHECKS if row.token not in declared)
    assert not missing, missing
