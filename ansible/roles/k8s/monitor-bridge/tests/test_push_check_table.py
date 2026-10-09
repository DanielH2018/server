"""`monitor_bridge_push_checks` in defaults/main.yml against the registry, the env and SOPS (#3659).

The table is what the deploy knows about each check: its name, from which the env-secret derives
`KUMA_PUSH_<NAME>`, and the SOPS secret holding its token. These replace two tests that regexed
`KUMA_PUSH_*` literals out of `files/` and the env-secret source and held the copies equal, plus
an etcd-drill test that held one such pair. With the env name derived there is no copy left to
compare, so what remains to check is that the three lists name the same checks.

The Kuma side, that each row's token reaches a push tile, is
`ansible/tests/services/test_kuma_static_monitors.py::test_every_bridge_push_token_reaches_a_push_tile`.
"""

from pathlib import Path

import gates
import registry
from bridge.types import push_env
from lib import yaml_fast
from lib.repo_paths import ANSIBLE

from _bridge_env import bridge_env

_ROLE = Path(__file__).resolve().parents[1]


def _rows() -> list[dict]:
    defaults = yaml_fast.safe_load((_ROLE / "defaults/main.yml").read_text())
    return defaults["monitor_bridge_push_checks"]


def test_the_table_names_every_registered_check_and_every_gate_once():
    names = [row["name"] for row in _rows()]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
    registered = {c.name for c in registry.build_checks({})}
    # Named members, so an empty registry or gate map cannot make both sides equal and empty.
    assert {"disk", "traefik_421"} <= registered
    assert "prometheus" in gates.GATE_DEPENDENTS
    assert set(names) == registered | set(gates.GATE_DEPENDENTS), (
        "only in the table: %s ; only in the registry or gates: %s"
        % (
            sorted(set(names) - registered - set(gates.GATE_DEPENDENTS)),
            sorted(registered | set(gates.GATE_DEPENDENTS) - set(names)),
        )
    )


def test_the_env_secret_renders_one_token_per_row_under_the_name_the_bridge_reads():
    rendered = {key for key in bridge_env() if key.startswith("KUMA_PUSH_")}
    assert "KUMA_PUSH_MEMORY" in rendered  # a row whose secret predates the naming rule
    assert rendered == {push_env(row["name"]) for row in _rows()}


def test_every_row_names_a_secret_sops_declares():
    # SOPS encrypts values, not keys, so the key list is readable without the age key. A row
    # whose secret is missing fails the deploy's render at lookup('vars').
    declared = set(yaml_fast.safe_load((ANSIBLE / "vars/secrets.yml").read_text()))
    missing = sorted(row["token"] for row in _rows() if row["token"] not in declared)
    assert not missing, missing
