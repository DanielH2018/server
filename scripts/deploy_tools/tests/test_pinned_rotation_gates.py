"""`pinned_rotation_gates.py` against a fake kubectl, a registry in `tmp_path` and a patched
environment: each gate's clean/flagged pair, the stop order, and the exit code naming the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_pinned_rotation_gates.py
"""

import io
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _gates_fakes import fake_tools
from deploy_tools import pinned_rotation_gates as gates
from lib import kubectl, yaml_fast

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "secret-rotation.md"

NOW = 1_800_000_000.0
HOUR = 3600.0
VOLUME = "pvc-4ee9f2af-9a09-4011-994f-57395434ae28"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _volume(name: str, namespace: str, pvc: str) -> dict:
    return {
        "metadata": {"name": name},
        "status": {"kubernetesStatus": {"namespace": namespace, "pvcName": pvc}},
    }


def _snapshot(volume: str, epoch: float, ready=True, recurring: str | None = None):
    labels = {"RecurringJob": recurring} if recurring else {}
    return {
        "metadata": {"name": f"snap-{int(epoch)}"},
        "spec": {"volume": volume},
        "status": {"readyToUse": ready, "creationTime": _iso(epoch), "labels": labels},
    }


def _deployment(available: int) -> dict:
    return {
        "metadata": {"name": "authelia"},
        "status": {"availableReplicas": available},
    }


VOLUMES = {"items": [_volume(VOLUME, "homelab", "authelia-config")]}
FRESH = {"items": [_snapshot(VOLUME, NOW - HOUR)]}
PINNED = {"entries": {gates.SECRET: {"last_rotated": "2025-06-27", "tier": "pinned"}}}
AVAILABLE = _deployment(1)


def _registry_yaml(tier: str) -> str:
    return f"entries:\n  {gates.SECRET}:\n    last_rotated: '2025-06-27'\n    tier: {tier}\n"


@pytest.fixture(autouse=True)
def _fresh_identity():
    kubectl.forget_served_cluster()
    yield
    kubectl.forget_served_cluster()


@pytest.fixture
def plain_shell(tmp_path):
    """The server kubeconfig a plain shell on daniel-box sees; the environ is passed empty."""
    kubeconfig = tmp_path / "k3s.yaml"
    kubeconfig.write_text("")
    return kubeconfig


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "secret_rotation.yml"
    path.write_text(_registry_yaml("pinned"))
    return path


def _run(registry, kubeconfig, snapshots=FRESH, deployment=AVAILABLE, environ=None):
    tools = fake_tools(
        {
            gates.VOLUMES_ARGS: VOLUMES,
            gates.SNAPSHOTS_ARGS: snapshots,
            gates.DEPLOYMENT_ARGS: deployment,
        }
    )
    out = io.StringIO()
    code = gates.run_gates(
        tools=tools,
        registry=registry,
        environ={} if environ is None else environ,
        server_kubeconfig=kubeconfig,
        now=NOW,
        out=out,
    )
    return code, out.getvalue()


# ── gate 1: the registry row ────────────────────────────────────────────────────────────────


def test_a_pinned_row_is_clean():
    assert gates.not_pinned(PINNED) == []


def test_the_real_registry_still_pins_the_secret():
    # Non-vacuity: the gate reads `ansible/secret_rotation.yml`, and that file must keep
    # carrying the row this script exists for.
    assert gates.not_pinned(yaml_fast.safe_load(gates.REGISTRY.read_text())) == []


def test_another_tier_a_record_key_or_a_missing_row_is_flagged():
    row = {"last_rotated": "2025-06-27", "tier": "auto", "source": "record"}
    assert gates.not_pinned({"entries": {gates.SECRET: row}}) == [
        f"{gates.SECRET} is tier auto, not pinned",
        f"{gates.SECRET} is a `source: record` key — the app holds the credential",
    ]
    assert "not in the registry" in gates.not_pinned({"entries": {}})[0]


# ── gate 2: the snapshot ────────────────────────────────────────────────────────────────────


def test_a_fresh_hand_taken_snapshot_is_clean():
    assert gates.no_fresh_snapshot(VOLUMES, FRESH, now=NOW) == []


def test_a_recurring_stale_or_unready_snapshot_does_not_count():
    doc = {
        "items": [
            _snapshot(VOLUME, NOW - 600, recurring="daily-backup"),
            _snapshot(VOLUME, NOW - 300, ready=False),
            _snapshot("pvc-other", NOW - 60),
        ]
    }
    found = gates.no_fresh_snapshot(VOLUMES, doc, now=NOW)
    assert len(found) == 1 and "take one in the Longhorn UI first" in found[0]
    stale = {"items": [_snapshot(VOLUME, NOW - 3 * HOUR)]}
    assert gates.no_fresh_snapshot(VOLUMES, stale, now=NOW) == [
        "the newest hand-taken snapshot of authelia-config is 3.0 h old (window 2 h) — "
        "take a fresh one"
    ]


def test_a_pvc_with_no_volume_is_flagged():
    found = gates.no_fresh_snapshot({"items": []}, FRESH, now=NOW)
    assert found == ["no Longhorn volume is bound to homelab/authelia-config"]


# ── gate 3: authelia available ──────────────────────────────────────────────────────────────


def test_an_available_replica_is_clean():
    assert gates.authelia_unavailable(_deployment(1)) == []


def test_no_available_replica_is_flagged():
    assert gates.authelia_unavailable(_deployment(0)) == [
        "authelia has 0 available replicas"
    ]
    assert gates.authelia_unavailable({"status": {}}) == [
        "authelia has 0 available replicas"
    ]


# ── gate 4: the shell ───────────────────────────────────────────────────────────────────────


def test_a_plain_shell_on_the_server_node_is_clean(plain_shell):
    assert gates.wrong_shell({}, plain_shell) == []


def test_a_claude_session_or_another_host_is_flagged(plain_shell, tmp_path):
    found = gates.wrong_shell({gates.CLAUDE_SESSION_VAR: "1"}, tmp_path / "absent.yaml")
    assert len(found) == 2
    assert "run the commands on daniel-box" in found[0]
    assert "transcribed" in found[1]


def test_a_failed_read_never_passes_a_gate():
    assert gates.no_fresh_snapshot(None, FRESH, now=NOW)
    assert gates.no_fresh_snapshot(VOLUMES, None, now=NOW)
    assert gates.authelia_unavailable(None)


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero(registry, plain_shell):
    code, out = _run(registry, plain_shell)
    assert code == 0, out
    assert out.count(" ok — ") == 4


def test_an_auto_tier_row_stops_at_gate_one_before_any_cluster_read(
    registry, plain_shell
):
    registry.write_text(_registry_yaml("auto"))
    code, out = _run(registry, plain_shell)
    assert code == 1
    assert "gate 2" not in out


@pytest.mark.parametrize(
    ("number", "kwargs"),
    [
        (2, {"snapshots": {"items": []}}),
        (3, {"deployment": _deployment(0)}),
    ],
)
def test_the_exit_code_is_the_first_failing_gate(registry, plain_shell, number, kwargs):
    code, out = _run(registry, plain_shell, **kwargs)
    assert code == number
    assert f"gate {number} FAILED" in out
    assert f"gate {number + 1} " not in out


def test_a_claude_session_is_the_last_gate_to_refuse(registry, plain_shell):
    # The three state gates still report from a Claude session; only the all-clear is withheld.
    code, out = _run(registry, plain_shell, environ={gates.CLAUDE_SESSION_VAR: "1"})
    assert code == 4
    assert out.count(" ok — ") == 3


def test_the_exit_codes_are_the_gate_positions():
    assert [g.number for g in gates.GATES] == [1, 2, 3, 4]
    assert gates.GATES[3].check is gates._gate_shell


# ── the runbook names the script ────────────────────────────────────────────────────────────


def test_the_runbook_calls_the_script_and_it_runs():
    script = "scripts/deploy_tools/pinned_rotation_gates.py"
    assert script in _RUNBOOK.read_text()
    proc = subprocess.run(
        ["uv", "run", "python", str(_REPO / script), "--bogus"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert proc.returncode == 64, proc.stderr
