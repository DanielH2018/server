"""`longhorn_upgrade_gates.py` against a fake kubectl: each gate's clean/flagged pair, the stop
order, and the exit code naming the gate.

Run: uv run pytest scripts/deploy_tools/tests/test_longhorn_upgrade_gates.py
"""

import io
import subprocess
from pathlib import Path

import pytest
from _gates_fakes import fake_tools, failing_read
from deploy_tools import longhorn_upgrade_gates as gates
from lib import kubectl, yaml_fast

_REPO = Path(__file__).resolve().parents[3]
_RUNBOOK = _REPO / "docs" / "longhorn-upgrade.md"

NOW = 1_800_000_000.0
DAY = 86400.0


def _target(name: str, url: str, available) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"backupTargetURL": url},
        "status": {"available": available},
    }


def _volume(name: str, state: str = "attached", robustness: str = "healthy") -> dict:
    return {
        "metadata": {"name": name},
        "status": {"state": state, "robustness": robustness},
    }


def _engine(image: str, state: str = "deployed", refs: int = 0) -> dict:
    return {
        "metadata": {"name": image.rsplit(":", 1)[-1]},
        "spec": {"image": image},
        "status": {"state": state, "refCount": refs},
    }


ARMED = {
    "items": [
        _target("default", "s3://bucket@us-east-005/longhorn", True),
        _target("r2", "s3://bucket@auto/longhorn", True),
    ]
}
SETTLED_VOLUMES = {"items": [_volume("pvc-a"), _volume("pvc-b", "detached", "unknown")]}
ONE_ENGINE = {"items": [_engine("longhornio/longhorn-engine:v1.12.1", refs=176)]}


@pytest.fixture(autouse=True)
def _fresh_identity():
    kubectl.forget_served_cluster()
    yield
    kubectl.forget_served_cluster()


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "longhorn-restore-drill"
    d.mkdir()
    (d / gates.DRILL_STAMP).write_text(f"{int(NOW - DAY)}\n")
    return d


def _run(
    state_dir, targets=ARMED, volumes=SETTLED_VOLUMES, engines=ONE_ENGINE, now=NOW
):
    tools = fake_tools(
        {
            gates.TARGETS_ARGS: targets,
            gates.VOLUMES_ARGS: volumes,
            gates.ENGINE_IMAGES_ARGS: engines,
        }
    )
    out = io.StringIO()
    code = gates.run_gates(
        tools=tools, state_dir=str(state_dir), max_age_s=3 * DAY, now=now, out=out
    )
    return code, out.getvalue()


# ── gate 1: the backup target ───────────────────────────────────────────────────────────────


def test_an_armed_available_target_is_clean():
    assert gates.unreachable_targets(ARMED, gates.REQUIRED_TARGETS) == []


def test_a_disarmed_default_or_an_unavailable_armed_target_is_flagged():
    doc = {
        "items": [
            _target("default", "", False),
            _target("r2", "s3://bucket@auto/longhorn", False),
        ]
    }
    assert gates.unreachable_targets(doc, gates.REQUIRED_TARGETS) == [
        "default (disarmed: backupTargetURL is empty)",
        "r2 (armed, available=False)",
    ]


def test_a_disarmed_target_the_runbook_does_not_need_is_clean():
    doc = {
        "items": [
            _target("default", "s3://bucket@us-east-005/longhorn", True),
            _target("r2", "", False),
        ]
    }
    assert gates.unreachable_targets(doc, gates.REQUIRED_TARGETS) == []


# ── gate 2: the restore drill ───────────────────────────────────────────────────────────────


def test_a_fresh_drill_stamp_is_clean(state_dir):
    assert gates.stale_restore_drill(state_dir, 3 * DAY, now=NOW) == []


def test_a_stale_or_absent_drill_stamp_is_flagged(state_dir):
    (state_dir / gates.DRILL_STAMP).write_text(f"{int(NOW - 4 * DAY)}\n")
    found = gates.stale_restore_drill(state_dir, 3 * DAY, now=NOW)
    assert found == ["last restore drill passed 4.0 days ago (window 3 days)"]
    (state_dir / gates.DRILL_STAMP).unlink()
    assert "has ever passed" in gates.stale_restore_drill(state_dir, 3 * DAY, NOW)[0]


def test_an_absent_state_directory_is_not_a_fresh_drill(tmp_path):
    found = gates.stale_restore_drill(tmp_path / "nowhere", 3 * DAY, now=NOW)
    assert len(found) == 1 and "not a directory" in found[0]


def test_the_window_is_the_k3s_roles_own_number():
    # One source for the threshold: the same default monitor-bridge's check 7 is rendered from.
    defaults = yaml_fast.safe_load(gates.K3S_DEFAULTS.read_text())
    assert gates.restore_drill_max_age_s() == defaults[gates.MAX_AGE_KEY] * DAY > 0


# ── gate 3: every volume accounted for ──────────────────────────────────────────────────────


def test_settled_healthy_volumes_are_clean():
    assert gates.unaccounted_volumes(SETTLED_VOLUMES) == []


def test_a_mid_flight_or_degraded_volume_is_flagged():
    doc = {
        "items": [
            _volume("pvc-x", "attaching"),
            _volume("pvc-y", robustness="degraded"),
        ]
    }
    assert gates.unaccounted_volumes(doc) == [
        "pvc-x (state=attaching)",
        "pvc-y (degraded)",
    ]


# ── gate 4: the engine images ───────────────────────────────────────────────────────────────


def test_one_deployed_engine_image_is_clean():
    assert gates.lagging_engine_images(ONE_ENGINE) == []


def test_a_second_referenced_image_or_an_undeployed_one_is_flagged():
    doc = {
        "items": [
            _engine("longhornio/longhorn-engine:v1.11.3", refs=3),
            _engine("longhornio/longhorn-engine:v1.12.1", state="deploying", refs=170),
        ]
    }
    assert gates.lagging_engine_images(doc) == [
        "longhornio/longhorn-engine:v1.12.1 (state=deploying)",
        "longhornio/longhorn-engine:v1.11.3 (3 references)",
        "longhornio/longhorn-engine:v1.12.1 (170 references)",
    ]


def test_a_failed_read_never_passes_a_gate():
    assert gates.unreachable_targets(None, gates.REQUIRED_TARGETS)
    assert gates.unaccounted_volumes(None)
    assert gates.lagging_engine_images(None)


# ── the runner: order, stop, exit code ──────────────────────────────────────────────────────


def test_all_gates_passing_exits_zero(state_dir):
    code, out = _run(state_dir)
    assert code == 0, out
    assert out.count(" ok — ") == 4


@pytest.mark.parametrize(
    ("number", "docs"),
    [
        (1, {"targets": {"items": [_target("default", "", False)]}}),
        (3, {"volumes": {"items": [_volume("pvc-bad", robustness="faulted")]}}),
        (
            4,
            {
                "engines": {
                    "items": [
                        _engine("e:v1", refs=1),
                        _engine("e:v2", refs=1),
                    ]
                }
            },
        ),
    ],
)
def test_the_exit_code_is_the_first_failing_gate(state_dir, number, docs):
    code, out = _run(state_dir, **docs)
    assert code == number
    assert f"gate {number} FAILED" in out
    assert f"gate {number + 1} " not in out


def test_a_stale_drill_stops_at_gate_two_before_the_volume_read(state_dir):
    code, out = _run(state_dir, now=NOW + 10 * DAY)
    assert code == 2
    assert "gate 3" not in out


def test_a_list_that_returns_nothing_is_unavailable_not_a_failed_gate(state_dir):
    tools = failing_read(
        fake_tools({gates.TARGETS_ARGS: ARMED}), "backuptargets.longhorn.io"
    )
    out = io.StringIO()
    code = gates.run_gates(tools=tools, state_dir=str(state_dir), out=out)
    assert code == gates.runbook_gates.EX_UNAVAILABLE
    assert "returned no document" in out.getvalue()


def test_the_exit_codes_are_the_gate_positions():
    assert [g.number for g in gates.GATES] == [1, 2, 3, 4]
    assert gates.GATES[1].check is gates._gate_drill


# ── the runbook names the script ────────────────────────────────────────────────────────────


def test_the_runbook_calls_the_script_and_it_runs():
    """`docs/longhorn-upgrade.md` must name this entry point, and the entry point must import.

    Running the script with a bad flag exercises its own `sys.path` bootstrap, which the
    in-process import above cannot.
    """
    script = "scripts/deploy_tools/longhorn_upgrade_gates.py"
    assert script in _RUNBOOK.read_text()
    proc = subprocess.run(
        ["uv", "run", "python", str(_REPO / script), "--bogus"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert proc.returncode == 64, proc.stderr
