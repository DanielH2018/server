"""The host-side checks of `k3s_gates` and the three modes: what each mode runs and refuses.

The host checks read files under `--host-root`. In a SUC `prepare` container that is `/host`,
the node's root, so a symlink in the path must resolve under it, not under the container.
"""

import ast
import fcntl
import os
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
from deployer_lib import deploy_locks

import gitops_markers
import k3s_gates

SCRIPT = Path(k3s_gates.__file__)
# The scripts that must never create or write the deploy lock. The restart script's own AST check
# is in test_k3s_restart.py, because it writes its state files, which this check forbids.
LOCK_READERS = (SCRIPT,)


@pytest.fixture
def root(tmp_path) -> Path:
    """A fake node root: the lock behind an absolute `/var/lock` link, a deployer state dir."""
    (tmp_path / "run/lock").mkdir(parents=True)
    (tmp_path / "run/lock/server-deploy-all.lock").touch()
    (tmp_path / "var").mkdir()
    (tmp_path / "var/lock").symlink_to("/run/lock")
    (tmp_path / "var/lib/gitops-deploy").mkdir(parents=True)
    return tmp_path


def _lock_file(root: Path) -> Path:
    return root / "run/lock/server-deploy-all.lock"


def _hold(root: Path, text: str) -> None:
    (root / "var/lib/gitops-deploy/hold_sha").write_text(text)


def _round(mode, routes, root, version=VERSION, guard=False, notes=None):
    checks = k3s_gates.checks_for(
        mode, fake_get(routes), str(root), version, lambda: NOW, guard, notes or []
    )
    return k3s_gates.run_round(checks)


# ── names the script shares with the deployer ───────────────────────────────────────────────


def test_the_lock_path_equals_the_deployers(monkeypatch):
    monkeypatch.delenv("HOMELAB_DEPLOY_LOCK_DIR", raising=False)
    assert k3s_gates.ALL_LOCK == deploy_locks.lock_path(deploy_locks.SERVICE_LOCK_ALL)


def test_a_lock_path_that_drifted_would_not_equal_the_deployers(monkeypatch):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", "/elsewhere")
    assert k3s_gates.ALL_LOCK != deploy_locks.lock_path(deploy_locks.SERVICE_LOCK_ALL)


def test_the_hold_marker_and_directory_equal_the_deployers():
    assert k3s_gates.HOLD_FILE == gitops_markers.MARKERS["hold"]
    assert k3s_gates.HOLD_DIR == gitops_markers.STATE_DIR


# ── resolving a path under the host root ────────────────────────────────────────────────────


def test_var_lock_resolves_under_the_fixture_root_not_the_tests_run_lock(root):
    resolved = k3s_gates.resolve_under(str(root), k3s_gates.ALL_LOCK)
    assert resolved == str(_lock_file(root))


def test_a_relative_link_and_dotdot_stay_inside_the_root(root):
    (root / "var/rel").symlink_to("../run/lock")
    assert k3s_gates.resolve_under(str(root), "/var/rel/../../etc") == str(root / "etc")


def test_a_symlink_loop_is_an_error(root):
    (root / "a").symlink_to("/b")
    (root / "b").symlink_to("/a")
    with pytest.raises(OSError, match="too many symlinks"):
        k3s_gates.resolve_under(str(root), "/a")


# ── the deploy lock ─────────────────────────────────────────────────────────────────────────


def test_a_free_lock_is_clean(root):
    assert k3s_gates.check_deploy_lock(str(root)) == []


def test_a_held_lock_is_refused_through_the_var_lock_link(root):
    fd = os.open(_lock_file(root), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert k3s_gates.check_deploy_lock(str(root)) == ["a deploy holds the all lock"]
    finally:
        os.close(fd)
    assert k3s_gates.check_deploy_lock(str(root)) == []


def test_a_shared_holder_blocks_the_exclusive_test(root):
    """A scoped deploy takes `all` shared, and the exclusive test must still see it."""
    fd = os.open(_lock_file(root), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        assert k3s_gates.check_deploy_lock(str(root)) != []
    finally:
        os.close(fd)


def test_an_absent_lock_file_is_refused_and_not_created(root):
    _lock_file(root).unlink()
    found = k3s_gates.check_deploy_lock(str(root))
    assert "absent" in found[0]
    assert not _lock_file(root).exists()


def test_the_lock_is_opened_read_only(root, monkeypatch):
    flags = []
    real_open = os.open

    def spy(path, flag, *args, **kwargs):
        flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    assert k3s_gates.check_deploy_lock(str(root)) == []
    assert flags == [os.O_RDONLY]


@pytest.mark.skipif(os.geteuid() != 0, reason="chown to another uid needs root")
def test_a_lock_owned_by_another_user_in_a_sticky_directory_is_opened_and_flocked(root):
    """`fs.protected_regular=2` refuses a root O_CREAT open of this file; O_RDONLY is exempt."""
    lock_dir = _lock_file(root).parent
    lock_dir.chmod(0o1777)
    os.chown(_lock_file(root), 65534, 65534)
    _lock_file(root).chmod(0o644)
    assert k3s_gates.check_deploy_lock(str(root)) == []


def _lock_opener_violations(source: str) -> list[str]:
    """`os.open` calls that can create or write, and write-mode opens, in `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {
            "O_CREAT",
            "O_WRONLY",
            "O_RDWR",
        }:
            found.append(f"{node.attr} at line {node.lineno}")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [
                k.value.value
                for k in node.keywords
                if k.arg == "mode" and isinstance(k.value, ast.Constant)
            ]
            if name == "open" and any(
                isinstance(m, str) and set(m) & set("wax+") for m in modes
            ):
                found.append(f"write-mode open at line {node.lineno}")
            if name in {"write_text", "write_bytes"}:
                found.append(f"{name} at line {node.lineno}")
    return found


def test_the_lock_readers_never_create_or_write():
    assert SCRIPT in LOCK_READERS and SCRIPT.is_file()
    source = SCRIPT.read_text()
    assert "os.open(" in source, "the opener this test polices is gone"
    for path in LOCK_READERS:
        assert _lock_opener_violations(path.read_text()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)",
        "os.open(path, os.O_WRONLY)",
        'open(path, "w")',
        'open(path, mode="a")',
        'Path(path).write_text("")',
    ],
)
def test_a_lock_opener_that_creates_or_writes_is_flagged(mutation):
    source = SCRIPT.read_text().replace(
        "fd = os.open(path, os.O_RDONLY)", f"fd = {mutation}", 1
    )
    assert mutation in source
    assert _lock_opener_violations(source)


def test_the_script_parses_with_the_python_312_grammar():
    """The hosts run it under Ubuntu 24.04's python3; ruff's default target would write 3.14."""
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


# ── the deployer's hold ─────────────────────────────────────────────────────────────────────


def test_an_absent_hold_file_is_clean(root):
    assert k3s_gates.check_deployer_hold(str(root)) == []


def test_an_empty_hold_file_is_clean(root):
    _hold(root, "\n")
    assert k3s_gates.check_deployer_hold(str(root)) == []


def test_a_held_sha_is_refused(root):
    _hold(root, "abc123\n")
    assert k3s_gates.check_deployer_hold(str(root)) == ["the deployer holds abc123"]


def test_an_absent_state_directory_is_refused(root):
    (root / "var/lib/gitops-deploy").rmdir()
    assert k3s_gates.check_deployer_hold(str(root))


def test_an_unreadable_hold_file_is_refused(root):
    (root / "var/lib/gitops-deploy/hold_sha").mkdir()
    assert "cannot read hold_sha" in k3s_gates.check_deployer_hold(str(root))[0]


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
    assert _names("server") == storage | {
        "restart marker",
        "server restart",
        "deployer hold",
        "deploy lock",
    }
    assert _names("agent") == storage | {
        "server plan",
        "restart marker",
        "server restart",
    }
    assert _names("host") == storage | {"suc idle", "flux settled"}
    assert _names("host", guard=True) == _names("host") | {"server restart"}


def test_the_server_mode_passes_on_a_healthy_cluster(root):
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


def test_the_server_mode_refuses_a_hold_and_a_held_lock(root):
    _hold(root, "abc")
    assert [
        f for f in _round("server", healthy(), root) if f.startswith("deployer hold:")
    ]
    _hold(root, "")
    fd = os.open(_lock_file(root), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert [
            f for f in _round("server", healthy(), root) if f.startswith("deploy lock:")
        ]
    finally:
        os.close(fd)


def test_the_agent_mode_passes_and_refuses(root):
    assert _round("agent", healthy(), root, version=MUNGED) == []
    assert _round("agent", healthy(), root, version=None) != []
    assert _round("agent", with_routes(lease=lease(acquired_s=300)), root) != []


def test_the_host_mode_reads_neither_the_hold_nor_the_lock(root):
    """The restart unit holds the `all` lock through the gates, so host mode must not test it."""
    _hold(root, "abc")
    fd = os.open(_lock_file(root), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert _round("host", healthy(), root) == []
    finally:
        os.close(fd)


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
