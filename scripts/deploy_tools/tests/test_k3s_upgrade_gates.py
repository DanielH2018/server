"""`k3s_upgrade_gates.py` against a fake kubectl: each gate's clean/flagged pair, the stop order,
and the exit code naming the gate.

The runner takes a `lib.kubectl.Tools`, so the cluster reads are answered from canned JSON
keyed on the kubectl arguments and nothing touches a live API server. The hold marker is
read from a `tmp_path` state directory through `GITOPS_STATE_DIR`'s override.

Run: uv run pytest scripts/deploy_tools/tests/test_k3s_upgrade_gates.py
"""

import io
import subprocess
from pathlib import Path

import pytest
from _gates_fakes import fake_tools
from deploy_tools import k3s_upgrade_gates as gates
from lib import kubectl
from lib.gitops_markers import MARKERS

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "k3s-upgrade.md"


def _volume(name: str, robustness: str, state: str = "attached") -> dict:
    return {
        "metadata": {"name": name},
        "status": {"state": state, "robustness": robustness},
    }


def _backup(name: str, state: str) -> dict:
    return {"metadata": {"name": name}, "status": {"state": state}}


def _node(name: str, ready: str = "True") -> dict:
    return {
        "metadata": {"name": name},
        "status": {"conditions": [{"type": "Ready", "status": ready}]},
    }


HEALTHY_VOLUMES = {
    "items": [_volume("pvc-a", "healthy"), _volume("pvc-b", "unknown", "detached")]
}
SETTLED_BACKUPS = {
    "items": [_backup("backup-1", "Completed"), _backup("backup-2", "Error")]
}
BOTH_READY = {"items": [_node("daniel-box"), _node("daniel-server")]}


def _tools(
    volumes=HEALTHY_VOLUMES, backups=SETTLED_BACKUPS, nodes=BOTH_READY
) -> kubectl.Tools:
    """A `Tools` whose kubectl answers the three reads from the given documents."""
    return fake_tools(
        {
            gates.VOLUMES_ARGS: volumes,
            gates.BACKUPS_ARGS: backups,
            gates.NODES_ARGS: nodes,
        }
    )


@pytest.fixture(autouse=True)
def _fresh_identity():
    # The served-cluster answer is cached per Tools; every test builds its own, but a
    # cleared cache keeps one test's fake from ever answering another's.
    kubectl.forget_served_cluster()
    yield
    kubectl.forget_served_cluster()


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "gitops-deploy"
    d.mkdir()
    return d


def _run(state_dir, **docs) -> tuple[int, str]:
    out = io.StringIO()
    code = gates.run_gates(tools=_tools(**docs), state_dir=str(state_dir), out=out)
    return code, out.getvalue()


# ── the four verdicts ───────────────────────────────────────────────────────────────────────


def test_a_healthy_or_idle_volume_is_clean():
    assert gates.unsafe_volumes(HEALTHY_VOLUMES) == []


def test_a_degraded_or_faulted_volume_is_flagged():
    doc = {
        "items": [
            _volume("pvc-ok", "healthy"),
            _volume("pvc-bad", "degraded"),
            _volume("pvc-x", "faulted"),
        ]
    }
    assert gates.unsafe_volumes(doc) == ["pvc-bad (degraded)", "pvc-x (faulted)"]


def test_a_settled_backup_is_clean():
    assert gates.in_flight_backups(SETTLED_BACKUPS) == []


def test_an_in_flight_backup_is_flagged():
    doc = {
        "items": [
            _backup("b1", "InProgress"),
            _backup("b2", "Completed"),
            _backup("b3", ""),
        ]
    }
    assert gates.in_flight_backups(doc) == ["b1 (InProgress)", "b3 (no state yet)"]


def test_a_missing_or_empty_hold_marker_is_clean(state_dir):
    assert gates.held_sha(state_dir) == []
    (state_dir / MARKERS["hold"]).write_text("\n")
    assert gates.held_sha(state_dir) == []


def test_a_held_sha_is_flagged_with_its_plane(state_dir):
    (state_dir / MARKERS["hold"]).write_text("abc1234\n")
    assert gates.held_sha(state_dir) == ["abc1234"]
    (state_dir / MARKERS["hold_plane"]).write_text("initial_setup.yml k3s\n")
    assert gates.held_sha(state_dir) == ["abc1234 (initial_setup.yml k3s)"]


def test_an_absent_state_directory_is_not_a_cleared_hold(tmp_path):
    # daniel-server has no /var/lib/gitops-deploy at all; that must read as "wrong host",
    # never as "no hold".
    found = gates.held_sha(tmp_path / "nowhere")
    assert len(found) == 1 and "not a directory" in found[0]


def test_both_nodes_ready_is_clean():
    assert gates.nodes_not_ready(BOTH_READY) == []


def test_a_not_ready_or_missing_node_is_flagged():
    doc = {"items": [_node("daniel-box", "False")]}
    assert gates.nodes_not_ready(doc) == [
        "daniel-box (Ready=False)",
        "daniel-server (not in the cluster)",
    ]


def test_a_failed_read_never_passes_a_gate():
    assert gates.unsafe_volumes(None)
    assert gates.in_flight_backups(None)
    assert gates.nodes_not_ready(None)


def test_a_state_this_file_has_not_heard_of_is_flagged():
    # Allow-lists: a Longhorn rename must stop the upgrade, not pass it.
    assert gates.unsafe_volumes({"items": [_volume("pvc-new", "rebuilding")]}) == [
        "pvc-new (rebuilding)"
    ]
    assert gates.in_flight_backups({"items": [_backup("b", "Finalizing")]}) == [
        "b (Finalizing)"
    ]


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero(state_dir):
    code, out = _run(state_dir)
    assert code == 0
    assert out.count(" ok — ") == 4, out


@pytest.mark.parametrize(
    ("number", "docs"),
    [
        (1, {"volumes": {"items": [_volume("pvc-bad", "degraded")]}}),
        (2, {"backups": {"items": [_backup("b1", "Pending")]}}),
        (
            4,
            {
                "nodes": {
                    "items": [_node("daniel-box"), _node("daniel-server", "False")]
                }
            },
        ),
    ],
)
def test_the_exit_code_is_the_first_failing_gate(state_dir, number, docs):
    code, out = _run(state_dir, **docs)
    assert code == number
    assert f"gate {number} FAILED" in out
    # Nothing after the failing gate ran.
    assert f"gate {number + 1} " not in out


def test_a_held_sha_stops_at_gate_three_before_the_node_read(state_dir):
    (state_dir / MARKERS["hold"]).write_text("deadbeef\n")
    code, out = _run(state_dir)
    assert code == 3
    assert "deadbeef" in out
    assert "gate 4" not in out


def test_a_list_that_returns_nothing_is_unavailable_not_a_failed_gate(state_dir):
    tools = _tools()
    real_run = tools.run

    def run(argv, timeout):
        if any(arg == "volumes.longhorn.io" for arg in argv):
            return subprocess.CompletedProcess(argv, 1, "", "Forbidden")
        return real_run(argv, timeout)

    out = io.StringIO()
    code = gates.run_gates(
        tools=kubectl.Tools(run, tools.find_tool, tools.find_kubeconfig),
        state_dir=str(state_dir),
        out=out,
    )
    assert code == gates.EX_UNAVAILABLE
    assert "returned no document" in out.getvalue()


def test_the_wrong_cluster_is_refused_not_graded(state_dir):
    stage = {"items": [_node("daniel-stage")]}
    code, out = _run(state_dir, nodes=stage)
    assert code == gates.EX_UNAVAILABLE
    assert "cannot ask the cluster" in out


def test_the_exit_codes_are_the_gate_positions():
    # The runbook cites "gate 1" by number; a reorder would silently renumber the exit codes.
    assert [g.number for g in gates.GATES] == [1, 2, 3, 4]
    assert gates.GATES[2].check is gates._gate_hold


# ── the runbook names the script ────────────────────────────────────────────────────────────


def test_the_runbook_calls_the_script_and_it_runs():
    """`docs/k3s-upgrade.md` must name this entry point, and the entry point must import.

    `test_documented_paths_exist.py` only sees a `file:line` citation, so a bare path in the
    runbook is invisible to it. Running the script with a bad flag exercises its own
    `sys.path` bootstrap, which the in-process import above cannot.
    """
    script = "scripts/deploy_tools/k3s_upgrade_gates.py"
    assert script in _RUNBOOK.read_text()
    proc = subprocess.run(
        ["uv", "run", "python", str(_REPO / script), "--bogus"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert proc.returncode == 64, proc.stderr
