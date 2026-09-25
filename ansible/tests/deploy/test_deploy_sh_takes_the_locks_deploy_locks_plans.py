#!/usr/bin/env python3
"""`deploy_locks.py` names and orders the service locks; `deploy.sh` only takes them.

Until 2026-09-18 both sides carried the naming and the order -- bash at
`take_service_locks`, Python at `service_locks` -- and a test compared the two, because
`sort` and Python's `sorted` disagree on `pihole` against `pi-peer-backup` unless the shell
pins `LC_ALL=C`, and a disagreement is a deadlock between a hand deploy and a tick. Since
#2054 the wrapper takes what `deploy_locks.plan` returns, in that order, so there is one
ordering to test rather than two to reconcile; since #2412 the wrapper is Python.

Three things are measured, each driven rather than read off the source:

- `plan` itself: `all` first, then every tag once in code-point order, and the mode `all` is
  taken in. The census is the real tag list, and the pair the two sorts disagreed on must be
  in it, or the order proves nothing.
- The deployer takes exactly what `plan` says: `service_locks` yields the same names in the
  same order, so a reordering inside it fails here.
- The wrapper takes exactly what `plan` says, WITHOUT sorting: fed a plan in reverse order,
  it flocks the files in that order. And the red half -- a `plan` that fails, or names
  nothing, leaves the wrapper refusing with exit 79 and no lock touched. Measured in process
  on `deploy_under_locks.take_service_locks`, handed a `plan`; `--detach` takes its locks
  through the same call, which a structural check pins.
- The MODE the wrapper takes `all` in, which decides whether a full run and a scoped run
  exclude each other. Taken over from the wall-clock pair in
  `scripts/deploy_tools/tests/test_deploy_service_lock_concurrency.py` (#2415): the property
  is about a lock mode, and a held lock in this process contends with a second descriptor on
  the same file exactly as another process would.

Run: uv run pytest ansible/tests/deploy/test_deploy_sh_takes_the_locks_deploy_locks_plans.py
"""

import ast
import fcntl
import os
import subprocess
import sys

import deploy_locks
import deploy_under_locks
import pytest
from _helpers import REPO
from deploy_tools.exit_codes import (
    DEPLOY_LOCK_BUSY,
    DEPLOY_LOCK_PLAN_FAILED,
    DEPLOY_SH_NO_VERDICT,
)

_DEPLOY_LOCKS = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_locks.py"
_DEPLOY_UNDER_LOCKS = REPO / "scripts" / "deploy_tools" / "deploy_under_locks.py"
_DEPLOY_DETACH = REPO / "scripts" / "deploy_tools" / "deploy_detach.py"
# The pair `sort` under a UTF-8 locale and Python's `sorted` order differently. Both are live
# roles, and a census that stopped finding them would prove the order on nothing.
_DISAGREEING_PAIR = ("pi-peer-backup", "pihole")


def _declared_tags() -> list[str]:
    """Every deploy tag, from the enumeration `deploy.sh` itself runs for a full run."""
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/deploy_tools/deploy_tags.py"), "list"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def _cli(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`deploy_locks.py <argv>` the way the wrapper runs it."""
    return subprocess.run(
        [sys.executable, str(_DEPLOY_LOCKS), *argv],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _plan_cli(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return _cli("plan", *args, env=env)


def _rows(stdout: str) -> list[tuple[str, ...]]:
    return [tuple(line.split("\t")) for line in stdout.splitlines() if line]


# -- plan ----------------------------------------------------------------------------------


def test_the_census_contains_the_pair_the_two_sorts_disagreed_on():
    """Non-vacuity: without these two, every ordering agrees and the order proves nothing."""
    tags = _declared_tags()
    missing = [tag for tag in _DISAGREEING_PAIR if tag not in tags]
    assert not missing, (
        f"{missing} are no longer deploy tags, so the ordering below is measured on a list "
        "no sort disagrees about. Find the pair a locale sort reorders and name it here."
    )


def test_plan_takes_all_shared_then_every_tag_once_in_code_point_order(tmp_path):
    """A scoped run's plan, over the real census handed over twice and unsorted."""
    tags = _declared_tags()
    result = _plan_cli(
        *reversed(tags), *tags, env={"HOMELAB_DEPLOY_LOCK_DIR": str(tmp_path)}
    )
    assert result.returncode == 0, result.stderr
    rows = _rows(result.stdout)
    assert rows[0] == ("all", "shared", str(tmp_path / "server-deploy-all.lock"))
    assert [name for name, _, _ in rows[1:]] == sorted(set(tags)), (
        "plan does not take the per-tag locks in code-point order, or takes one twice"
    )
    assert {mode for _, mode, _ in rows[1:]} == {"exclusive"}
    assert [path for _, _, path in rows[1:]] == [
        str(tmp_path / f"server-deploy-{name}.lock") for name in sorted(set(tags))
    ]


def test_plan_takes_all_exclusively_for_a_full_run(tmp_path):
    """`--exclusive-all` is the wrapper's full run: it must exclude every scoped run."""
    result = _plan_cli(
        "--exclusive-all",
        "beta",
        "alpha",
        env={"HOMELAB_DEPLOY_LOCK_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert _rows(result.stdout) == [
        ("all", "exclusive", str(tmp_path / "server-deploy-all.lock")),
        ("alpha", "exclusive", str(tmp_path / "server-deploy-alpha.lock")),
        ("beta", "exclusive", str(tmp_path / "server-deploy-beta.lock")),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ("plan",),
        ("plan", "--exclusive-all"),
        ("plan", "--sorted", "alpha"),
        ("list", "alpha"),
        (),
    ],
    ids=[
        "no-tags",
        "exclusive-all-no-tags",
        "unknown-option",
        "unknown-subcommand",
        "no-subcommand",
    ],
)
def test_plan_refuses_a_call_it_cannot_plan_and_prints_no_lock(argv):
    """FLAGGED half: a usage error exits 2 with nothing on stdout the wrapper could take.

    No tags is refused rather than planned as `all` alone -- the wrapper enumerates a full
    run's tags itself, so an empty list here is a wrapper bug, and `all` alone would let it
    deploy everything under one lock.
    """
    result = _cli(*argv)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "usage" in result.stderr


def test_lock_path_is_the_one_naming_site_and_sanitises_what_the_shell_did(
    tmp_path, monkeypatch
):
    """The shell substituted `_` for anything outside `[A-Za-z0-9_.-]`; the module does now."""
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    assert deploy_locks.lock_path("sonarr") == str(
        tmp_path / "server-deploy-sonarr.lock"
    )
    assert deploy_locks.lock_path("a/b c") == str(tmp_path / "server-deploy-a_b_c.lock")


# -- the deployer takes what plan says --------------------------------------------------------


def test_service_locks_takes_exactly_the_locks_plan_names_in_that_order(
    tmp_path, monkeypatch
):
    """Driven, not described: the context manager yields the names as it takes them."""
    tags = _declared_tags()
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    planned = [name for name, _, _ in deploy_locks.plan(tags)]
    with deploy_locks.service_locks(tags, timeout=30) as taken:
        assert taken == planned
    assert taken[0] == deploy_locks.SERVICE_LOCK_ALL
    assert taken[1:] == sorted(set(tags))


# -- the foreground takes what plan says, in process -----------------------------------------


def _locked_run(tags: list[str]) -> deploy_under_locks.Run:
    return deploy_under_locks.Run(repo_root=REPO, tags=tags, at_sha="", args=[])


def _taken(run: deploy_under_locks.Run) -> list[str]:
    """The lock files behind the run's descriptors, in the order it opened them."""
    return [
        os.path.basename(os.readlink(f"/proc/self/fd/{fd}")) for fd in run.service_fds
    ]


def test_the_foreground_takes_the_locks_in_the_order_plan_named_them(tmp_path):
    """Fed a reversed plan, the locked half takes it reversed: it does not sort, and it does
    not put `all` first on its own. Both are the plan's job, and only the plan's."""
    reversed_plan = [
        deploy_locks.PlannedLock(name, str(tmp_path / f"server-deploy-{name}.lock"), ex)
        for name, ex in (("zeta", True), ("alpha", True), ("all", False))
    ]
    run = _locked_run(["alpha"])
    try:
        deploy_under_locks.take_service_locks(
            run, [], plan=lambda tags, exclusive_all=False: reversed_plan
        )
        assert _taken(run) == [
            "server-deploy-zeta.lock",
            "server-deploy-alpha.lock",
            "server-deploy-all.lock",
        ], "the locked half reordered the plan, so its lock order is not the deployer's"
    finally:
        run.close()


def test_the_foreground_takes_the_real_plan_all_first_then_the_tags(
    tmp_path, monkeypatch
):
    """The same half on the real `plan`: what a scoped deploy of two tags really flocks."""
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    run = _locked_run(["pihole", "pi-peer-backup"])
    try:
        deploy_under_locks.take_service_locks(run, [])
        assert _taken(run) == [
            "server-deploy-all.lock",
            "server-deploy-pi-peer-backup.lock",
            "server-deploy-pihole.lock",
        ]
    finally:
        run.close()


def _hold(path, mode: int) -> int:
    """Flock `path` as another deploy would; the open descriptor to close afterwards.

    flock(2) locks belong to the open file description, so a second `open` in this process
    contends with this one — which is what lets the two cases below drive a real contention
    without a second process. `test_deploy_service_locks.py` holds its locks the same way.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    fcntl.flock(fd, mode | fcntl.LOCK_NB)
    return fd


def test_a_scoped_run_takes_the_all_lock_shared(tmp_path, monkeypatch):
    """CLEAN half: two scoped deploys of different services must not exclude each other.

    Another run already holds `all` shared. This one has to take it shared too, or every
    scoped deploy would queue behind every other — which is the whole of ADR-0017 undone. An
    exclusive take here would block and refuse with DEPLOY_LOCK_BUSY.
    """
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_WAIT", "1")
    held = _hold(tmp_path / "server-deploy-all.lock", fcntl.LOCK_SH)
    run = _locked_run(["alpha"])
    try:
        deploy_under_locks.take_service_locks(run, [])
        assert _taken(run) == ["server-deploy-all.lock", "server-deploy-alpha.lock"]
    finally:
        run.close()
        os.close(held)


def test_a_run_naming_no_service_waits_for_a_scoped_run(tmp_path, monkeypatch):
    """FLAGGED half for the same mode split: a full run applies the scoped run's service too.

    It wants `all` exclusively, so a scoped run's shared hold has to block it. Without the
    split this returns at once and a whole-playbook apply lands on a running service deploy.
    """
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_WAIT", "1")
    held = _hold(tmp_path / "server-deploy-all.lock", fcntl.LOCK_SH)
    run = _locked_run([])
    try:
        with pytest.raises(deploy_under_locks.Refused) as refusal:
            deploy_under_locks.take_service_locks(run, ["alpha"])
        assert refusal.value.code == DEPLOY_LOCK_BUSY
    finally:
        run.close()
        os.close(held)


def _plan_raises(tags, exclusive_all=False):
    raise RuntimeError("broken on purpose")


@pytest.mark.parametrize(
    "plan",
    [_plan_raises, lambda tags, exclusive_all=False: []],
    ids=["plan-raises", "plan-names-nothing"],
)
def test_the_foreground_refuses_with_79_and_takes_nothing_when_plan_fails(plan, capsys):
    """FLAGGED half: no plan, no locks -- never a fallback order of its own."""
    run = _locked_run(["alpha"])
    with pytest.raises(deploy_under_locks.Refused) as refused:
        deploy_under_locks.take_service_locks(run, [], plan=plan)
    assert refused.value.code == DEPLOY_LOCK_PLAN_FAILED
    assert DEPLOY_LOCK_PLAN_FAILED in DEPLOY_SH_NO_VERDICT
    assert run.service_fds == [], (
        "the locked half took a lock with no plan to take it from"
    )
    err = capsys.readouterr().err
    assert "nothing was deployed" in err
    assert "deploy_locks.plan" in err


# -- what the wrapper no longer carries -------------------------------------------------


def _non_docstring_strings(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_locked_half_neither_names_a_service_lock_nor_sorts_through_a_locale():
    """The same verify line for the Python half: its docstrings may say `server-deploy-`, its
    code may not, and nothing in it hands a tag list to `sort` under a locale."""
    strings = _non_docstring_strings(_DEPLOY_UNDER_LOCKS.read_text())
    naming = [s for s in strings if "server-deploy-" in s]
    assert naming == [], f"deploy_under_locks.py names a service lock itself: {naming}"
    sorting = [s for s in strings if "LC_ALL" in s or s == "sort"]
    assert sorting == [], f"deploy_under_locks.py sorts through a locale: {sorting}"


def test_the_string_census_sees_a_service_lock_literal():
    """Non-vacuity for the check above: a literal outside a docstring is found."""
    assert _non_docstring_strings(
        '"""server-deploy- in a docstring"""\nx = "server-deploy-all.lock"\n'
    ) == ["server-deploy-all.lock"]


def test_the_tree_lock_default_is_the_module_constant(monkeypatch):
    """The wrapper and the deployer must guard one file; driven, with no override set."""
    monkeypatch.delenv("HOMELAB_DEPLOY_TREE_LOCK", raising=False)
    assert deploy_under_locks.tree_lock_path() == deploy_locks.TREE_LOCK


def _service_lock_calls(source: str) -> dict[str, int]:
    """How often `source` calls `take_service_locks` and `plan` by any spelling."""
    counts = {"take_service_locks": 0, "plan": 0}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in counts:
                counts[name] += 1
    return counts


def test_detach_takes_its_service_locks_through_the_foregrounds_helper():
    """`--detach` plans nothing itself: the order tests above cover it through this call."""
    assert _service_lock_calls(_DEPLOY_DETACH.read_text()) == {
        "take_service_locks": 1,
        "plan": 0,
    }


def test_a_detach_that_plans_its_own_locks_is_flagged():
    assert _service_lock_calls(
        "def run(s):\n    for lock in deploy_locks.plan(s.tags):\n        take(lock)\n"
    ) == {"take_service_locks": 0, "plan": 1}
