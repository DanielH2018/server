"""`k3s_gates` and the runbook's own predicates agree on the same fixtures.

The runbook gates in `scripts/deploy_tools/runbook_gates_lib/` import repo code, so a host
cannot run them, and `k3s_gates.py` carries its own copy of each rule. Two copies of a safety
rule drift, so this module feeds one fixture to both and fails when they disagree. The mutation
half loads a copy of the script patched to accept a degraded volume and requires the
comparison to notice.
"""

import types
from pathlib import Path

import pytest
from _k3s_gates_fixtures import backup, fake_get, listing, node, volume, with_routes
from deploy_tools.runbook_gates_lib import gate_runner, k3s_upgrade
from lib.kubectl import CLUSTER_NODES

import k3s_gates

SCRIPT = Path(k3s_gates.__file__)

VOLUME_FIXTURES = {
    "healthy": listing(volume()),
    "idle-unknown": listing(volume(), volume("b", "unknown")),
    "one-degraded": listing(volume(), volume("b", "degraded")),
    "faulted": listing(volume("b", "faulted")),
    "unrecognised-state": listing(volume("b", "renamed")),
    "no-robustness": listing(volume("b", None)),
}
BACKUP_FIXTURES = {
    "completed": listing(backup()),
    "error-is-settled": listing(backup("b", "Error")),
    "in-progress": listing(backup("b", "InProgress")),
    "new": listing(backup("b", "New")),
    "no-state-yet": listing(backup("b", None)),
}
NODE_FIXTURES = {
    "both-ready": listing(node("daniel-box"), node("daniel-server")),
    "server-not-ready": listing(node("daniel-box"), node("daniel-server", "Unknown")),
    "server-missing": listing(node("daniel-box")),
    "box-missing": listing(node("daniel-server")),
}


def runbook_volumes(doc):
    return gate_runner.unsafe_volumes(doc)


def disagreements(gates) -> list[str]:
    """Fixture ids where `gates` and the runbook predicate return different verdicts."""
    found = []
    for name, doc in VOLUME_FIXTURES.items():
        refused = bool(gates.check_volumes(fake_get(with_routes(volumes=doc))))
        if refused != bool(runbook_volumes(doc)):
            found.append(f"volumes/{name}")
    for name, doc in BACKUP_FIXTURES.items():
        refused = bool(gates.check_backups(fake_get(with_routes(backups=doc))))
        if refused != bool(k3s_upgrade.in_flight_backups(doc)):
            found.append(f"backups/{name}")
    for name, doc in NODE_FIXTURES.items():
        refused = bool(gates.check_nodes(fake_get(with_routes(nodes=doc))))
        if refused != bool(k3s_upgrade.nodes_not_ready(doc)):
            found.append(f"nodes/{name}")
    return found


def load_source(source: str) -> types.ModuleType:
    """A module built from `source`, so a test can run a patched copy of the script."""
    module = types.ModuleType("k3s_gates_copy")
    exec(compile(source, "k3s_gates_copy.py", "exec"), module.__dict__)
    return module


def test_the_script_and_the_runbook_agree_on_every_fixture():
    assert disagreements(k3s_gates) == []


def test_the_fixture_set_has_both_verdicts_for_each_predicate():
    """Non-vacuity: a set where the runbook only ever passes would let any script agree."""
    assert any(runbook_volumes(d) for d in VOLUME_FIXTURES.values())
    assert not all(runbook_volumes(d) for d in VOLUME_FIXTURES.values())
    assert any(k3s_upgrade.in_flight_backups(d) for d in BACKUP_FIXTURES.values())
    assert not all(k3s_upgrade.in_flight_backups(d) for d in BACKUP_FIXTURES.values())
    assert any(k3s_upgrade.nodes_not_ready(d) for d in NODE_FIXTURES.values())
    assert not all(k3s_upgrade.nodes_not_ready(d) for d in NODE_FIXTURES.values())


def test_a_copy_that_accepts_a_degraded_volume_fails_the_parity_comparison():
    needle = 'SAFE_ROBUSTNESS = frozenset({"healthy", "unknown"})'
    source = SCRIPT.read_text()
    assert source.count(needle) == 1
    patched = load_source(
        source.replace(
            needle,
            'SAFE_ROBUSTNESS = frozenset({"healthy", "unknown", "degraded", "faulted"})',
        )
    )
    assert disagreements(patched) == ["volumes/one-degraded", "volumes/faulted"]


def test_a_copy_that_settles_an_in_progress_backup_fails_the_parity_comparison():
    needle = 'SETTLED_BACKUP = frozenset({"Completed", "Error", "Unknown"})'
    source = SCRIPT.read_text()
    assert source.count(needle) == 1
    patched = load_source(
        source.replace(
            needle,
            'SETTLED_BACKUP = frozenset({"Completed", "Error", "Unknown", "InProgress", "New", ""})',
        )
    )
    assert {"backups/in-progress", "backups/new", "backups/no-state-yet"} <= set(
        disagreements(patched)
    )


def test_the_allow_lists_equal_the_shared_definitions():
    from longhorn_robustness import SAFE_ROBUSTNESS

    assert k3s_gates.SAFE_ROBUSTNESS == SAFE_ROBUSTNESS
    assert k3s_gates.SETTLED_BACKUP == k3s_upgrade.SETTLED_BACKUP
    assert set(k3s_gates.NODES) == CLUSTER_NODES["prod"]


# ── where the script is deliberately stricter than the runbook ─────────────────────────────


def test_zero_volumes_is_refused_by_the_script_and_passed_by_the_runbook():
    """The runbook reads an empty list as no unsafe volume. A pod that sees none is blind."""
    empty = listing()
    assert runbook_volumes(empty) == []
    assert k3s_gates.check_volumes(fake_get(with_routes(volumes=empty))) != []


# ── held_sha ───────────────────────────────────────────────────────────────────────────────


def _runbook_refuses_hold(state_dir: Path) -> bool:
    try:
        return bool(k3s_upgrade.held_sha(state_dir))
    except gate_runner.Unreadable:
        return True


def _script_refuses_hold(state_dir: Path) -> bool:
    root = state_dir.parents[2]
    return bool(k3s_gates.check_deployer_hold(str(root)))


@pytest.fixture
def state(tmp_path) -> Path:
    path = tmp_path / "var/lib/gitops-deploy"
    path.mkdir(parents=True)
    return path


@pytest.mark.parametrize(
    "setup, refused",
    [
        (lambda d: None, False),
        (lambda d: (d / "hold_sha").write_text(""), False),
        (lambda d: (d / "hold_sha").write_text("abc123\n"), True),
        (lambda d: (d / "hold_sha").mkdir(), True),
        (lambda d: d.rmdir(), True),
    ],
    ids=["no-hold-file", "empty-hold-file", "held", "unreadable-hold", "no-state-dir"],
)
def test_the_hold_verdict_matches_held_sha(setup, refused, state):
    setup(state)
    assert _runbook_refuses_hold(state) is refused
    assert _script_refuses_hold(state) is refused
