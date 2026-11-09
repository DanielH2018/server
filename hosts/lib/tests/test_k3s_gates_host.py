"""The host-side checks of `k3s_gates` and the three modes: what each mode runs and refuses.

The host checks read files under `--host-root`. In a SUC `prepare` container that is `/host`,
the node's root, so a symlink in the path must resolve under it, not under the container.
"""

import ast
from pathlib import Path

import pytest
from _k3s_gates_fixtures import (
    MUNGED,
    NOW,
    VERSION,
    fake_get,
    healthy,
    lease,
    with_routes,
)
import k3s_gates

SCRIPT = Path(k3s_gates.__file__)


@pytest.fixture
def root(tmp_path) -> Path:
    """A fake node root: `/run/lock` behind an absolute `/var/lock` link, and no deployer."""
    (tmp_path / "run/lock").mkdir(parents=True)
    (tmp_path / "var").mkdir()
    (tmp_path / "var/lock").symlink_to("/run/lock")
    return tmp_path


def _round(mode, routes, root, version=VERSION, guard=False, notes=None):
    checks = k3s_gates.checks_for(
        mode, fake_get(routes), str(root), version, lambda: NOW, guard, notes or []
    )
    return k3s_gates.run_round(checks)


# ── resolving a path under the host root ────────────────────────────────────────────────────


def test_var_lock_resolves_under_the_fixture_root_not_the_tests_run_lock(root):
    resolved = k3s_gates.resolve_under(str(root), "/var/lock/a.lock")
    assert resolved == str(root / "run/lock/a.lock")


def test_a_relative_link_and_dotdot_stay_inside_the_root(root):
    (root / "var/rel").symlink_to("../run/lock")
    assert k3s_gates.resolve_under(str(root), "/var/rel/../../etc") == str(root / "etc")


def test_a_symlink_loop_is_an_error(root):
    (root / "a").symlink_to("/b")
    (root / "b").symlink_to("/a")
    with pytest.raises(OSError, match="too many symlinks"):
        k3s_gates.resolve_under(str(root), "/a")


def test_the_script_parses_with_the_python_312_grammar():
    """The hosts run it under Ubuntu 24.04's python3; ruff's default target would write 3.14."""
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


# ── the restart marker ──────────────────────────────────────────────────────────────────────


def test_the_marker_directory_is_refused_and_its_absence_is_clean(root):
    assert k3s_gates.check_restart_marker(str(root)) == []
    (root / "run/homelab-k3s-restart").mkdir()
    assert "exists" in k3s_gates.check_restart_marker(str(root))[0]


@pytest.mark.parametrize("mode", ["server", "agent"])
def test_the_marker_refuses_both_prepare_modes_and_its_absence_passes(mode, root):
    assert _round(mode, healthy(), root) == []
    (root / "run/homelab-k3s-restart").mkdir()
    found = _round(mode, healthy(), root)
    assert [f for f in found if f.startswith("restart marker:")]


def test_host_mode_passes_with_the_marker_present(root):
    (root / "run/homelab-k3s-restart").mkdir()
    assert _round("host", healthy(), root, guard=True) == []


# ── the modes ───────────────────────────────────────────────────────────────────────────────


def _names(mode, guard=False):
    checks = k3s_gates.checks_for(
        mode, fake_get(healthy()), "/", VERSION, lambda: NOW, guard, []
    )
    return {name for name, _ in checks}


def test_each_mode_runs_the_checks_the_plan_names():
    storage = {"volumes", "backups", "nodes", "coredns"}
    assert _names("server") == storage | {"restart marker", "server restart"}
    assert _names("agent") == storage | {
        "server plan",
        "restart marker",
        "server restart",
    }
    assert _names("host") == storage | {"suc idle", "flux settled"}
    assert _names("host", guard=True) == _names("host") | {"server restart"}


def test_the_server_mode_passes_on_a_healthy_cluster_with_no_deployer_state_or_lock(
    root,
):
    """A node with the deployer retired has neither `/var/lib/gitops-deploy` nor its lock file."""
    assert not (root / "var/lib/gitops-deploy").exists()
    assert list((root / "run/lock").iterdir()) == []
    assert _round("server", healthy(), root) == []


@pytest.mark.parametrize(
    "routes, prefix",
    [
        (with_routes(lease=lease(acquired_s=300)), "server restart:"),
        (with_routes(volumes={"items": []}), "volumes:"),
    ],
    ids=["server-restarted-5-minutes-ago", "no-volumes"],
)
def test_the_server_mode_refuses(routes, prefix, root):
    assert [f for f in _round("server", routes, root) if f.startswith(prefix)]


def test_the_agent_mode_passes_and_refuses(root):
    assert _round("agent", healthy(), root, version=MUNGED) == []
    assert _round("agent", healthy(), root, version=None) != []
    assert _round("agent", with_routes(lease=lease(acquired_s=300)), root) != []


@pytest.mark.parametrize(
    "guard, refused",
    [(True, True), (False, False)],
    ids=["daniel-server", "daniel-box"],
)
def test_the_host_mode_runs_the_server_restart_guard_only_where_asked(
    guard, refused, root
):
    routes = with_routes(lease=lease(acquired_s=300))
    assert bool(_round("host", routes, root, guard=guard)) is refused
