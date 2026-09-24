#!/usr/bin/env python3
"""`deploy_locks.py` names and orders the service locks; `deploy.sh` only takes them.

Until 2026-09-18 both sides carried the naming and the order -- bash at
`take_service_locks`, Python at `service_locks` -- and a test compared the two, because
`sort` and Python's `sorted` disagree on `pihole` against `pi-peer-backup` unless the shell
pins `LC_ALL=C`, and a disagreement is a deadlock between a hand deploy and a tick. Now the
shell runs `deploy_locks.py plan` and takes what it prints, in the order printed, so there is
one ordering to test rather than two to reconcile (issue #2054).

Three things are measured, each driven rather than read off the source:

- `plan` itself: `all` first, then every tag once in code-point order, and the mode `all` is
  taken in. The census is the real tag list, and the pair the two sorts disagreed on must be
  in it, or the order proves nothing.
- The deployer takes exactly what `plan` says: `service_locks` yields the same names in the
  same order, so a reordering inside it fails here.
- The wrapper takes exactly what `plan` says, WITHOUT sorting: fed a plan in reverse order,
  it flocks the files in that order. And the red half -- a `plan` that fails, or names
  nothing, leaves the wrapper refusing with exit 79 and no lock touched. A foreground run takes
  its locks in process (`deploy_under_locks.py`, handed a `plan`); `--detach` runs `deploy_locks.py
  plan` from bash, measured through a recording `flock`, until slice 4 of #2412.

Run: uv run pytest ansible/tests/deploy/test_deploy_sh_takes_the_locks_deploy_locks_plans.py
"""

import ast
import os
import subprocess
import sys

import deploy_locks
import deploy_under_locks
import pytest
from _deploy_sh_fakes import (
    FAKE_RECAP,
    UV_DEPLOY_RUN_ARM,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
    stub_bin,
)
from _helpers import REPO
from deploy_tools.exit_codes import DEPLOY_LOCK_PLAN_FAILED, DEPLOY_SH_NO_VERDICT

_DEPLOY_SH = REPO / "scripts" / "deploy.sh"
# The shell the text checks below read: the locked half behind the shim (#2412).
_DEPLOY_LOCKED = REPO / "scripts" / "deploy_tools" / "deploy_locked.sh"
_DEPLOY_LOCKS = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_locks.py"
_DEPLOY_UNDER_LOCKS = REPO / "scripts" / "deploy_tools" / "deploy_under_locks.py"
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


# -- the --detach arm takes what plan says ------------------------------------------------

# Records the file behind every descriptor `flock` is handed, then takes no lock. The wrapper
# flocks a DESCRIPTOR (`exec {fd}>"$path"; flock ... "$fd"`), so the path is readable only
# through /proc from inside the child, and this is the one place both the order and the names
# the wrapper really used can be observed.
_FLOCK_RECORDER = """#!/bin/bash
for arg in "$@"; do
  [[ "$arg" =~ ^[0-9]+$ ]] || continue
  readlink "/proc/$$/fd/$arg" >> "$DEPLOY_TEST_FLOCKS"
done
exit 0
"""

# A plan in the order NO sort would produce: `zeta` before `alpha`, and `all` last. The
# wrapper must take it as printed -- a wrapper that still sorted, or still put `all` first
# of its own accord, would take a different order from the one the deployer walks.
_UV_REVERSED_PLAN = """#!/bin/bash
case "$*" in
{run}
  *ansible-playbook*) {recap}; exit 0 ;;
  *deploy_locks.py*)
    printf 'zeta\\texclusive\\t%s/server-deploy-zeta.lock\\n' "$HOMELAB_DEPLOY_LOCK_DIR"
    printf 'alpha\\texclusive\\t%s/server-deploy-alpha.lock\\n' "$HOMELAB_DEPLOY_LOCK_DIR"
    printf 'all\\tshared\\t%s/server-deploy-all.lock\\n' "$HOMELAB_DEPLOY_LOCK_DIR"
    exit 0 ;;
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{run}", UV_DEPLOY_RUN_ARM)

_UV_PLAN_EXITS_NONZERO = """#!/bin/bash
case "$*" in
{run}
  *ansible-playbook*) touch "$DEPLOY_TEST_PLAYBOOK_RAN"; {recap}; exit 0 ;;
  *deploy_locks.py*) echo "deploy_locks.py: broken on purpose" >&2; exit 1 ;;
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{run}", UV_DEPLOY_RUN_ARM)

_UV_PLAN_PRINTS_NOTHING = """#!/bin/bash
case "$*" in
{run}
  *ansible-playbook*) touch "$DEPLOY_TEST_PLAYBOOK_RAN"; {recap}; exit 0 ;;
  *deploy_locks.py*) exit 0 ;;
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{run}", UV_DEPLOY_RUN_ARM)

_UV_REAL_PLAN = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)


# A finished run annotates itself through `logger`, which the leak guard shims: a fixture
# deploy must not land on the Landings board beside real ones.
_LOGGER_STUB = "#!/bin/bash\nexit 0\n"


def _run_detach(tmp_path, uv_stub: str, *args: str, **env: str):
    """`deploy.sh --detach`: the one arm still in bash, which runs `plan` as a subprocess."""
    bin_dir = stub_bin(
        tmp_path, {"uv": uv_stub, "flock": _FLOCK_RECORDER, "logger": _LOGGER_STUB}
    )
    repo = make_snapshot_repo(tmp_path / "repo")
    flocks = tmp_path / "flocks"
    flocks.touch()
    full_env = deploy_sh_env(
        tmp_path,
        bin_dir,
        DEPLOY_TEST_FLOCKS=str(flocks),
        DEPLOY_TEST_PLAYBOOK_RAN=str(tmp_path / "playbook-ran"),
        **env,
    )
    result = subprocess.run(
        [
            str(_DEPLOY_SH),
            "--detach",
            "--skip-tag-check",
            "--skip-staleness-check",
            *args,
        ],
        cwd=repo,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    service_flocks = [
        os.path.basename(line)
        for line in flocks.read_text().splitlines()
        if "server-deploy-" in line
    ]
    return result, service_flocks


def test_detach_takes_the_locks_in_the_order_plan_printed_them(tmp_path):
    """The bash arm's half of the reversed-plan test above."""
    result, service_flocks = _run_detach(tmp_path, _UV_REVERSED_PLAN, "--tags", "alpha")
    assert result.returncode == 0, result.stderr
    assert service_flocks == [
        "server-deploy-zeta.lock",
        "server-deploy-alpha.lock",
        "server-deploy-all.lock",
    ], "deploy.sh --detach reordered the plan, so its lock order is not the deployer's"


def test_detach_takes_the_real_plan_all_first_then_the_tags(tmp_path):
    """The bash arm on the real module: what a scoped --detach of two tags really flocks."""
    result, service_flocks = _run_detach(
        tmp_path, _UV_REAL_PLAN, "--tags", "pihole,pi-peer-backup"
    )
    assert result.returncode == 0, result.stderr
    assert service_flocks == [
        "server-deploy-all.lock",
        "server-deploy-pi-peer-backup.lock",
        "server-deploy-pihole.lock",
    ]


@pytest.mark.parametrize(
    "uv_stub",
    [_UV_PLAN_EXITS_NONZERO, _UV_PLAN_PRINTS_NOTHING],
    ids=["plan-exits-nonzero", "plan-prints-nothing"],
)
def test_detach_refuses_with_79_and_takes_nothing_when_plan_fails(tmp_path, uv_stub):
    """FLAGGED half: no plan, no locks, no playbook -- never a fallback order of its own."""
    result, service_flocks = _run_detach(tmp_path, uv_stub, "--tags", "alpha")
    assert result.returncode == DEPLOY_LOCK_PLAN_FAILED, (result.stdout, result.stderr)
    assert DEPLOY_LOCK_PLAN_FAILED in DEPLOY_SH_NO_VERDICT
    assert service_flocks == [], (
        "deploy.sh took a service lock with no plan to take it from"
    )
    assert not (tmp_path / "playbook-ran").exists()
    assert "nothing was deployed" in result.stderr
    assert "deploy_locks.py plan" in result.stderr


def test_detach_refuses_with_79_when_plan_hangs(tmp_path):
    """The bound: a plan that never answers is refused, not waited on. No lock is held while
    it runs, so the cost is this run alone -- but the run must still end. Bash only: the
    foreground imports `plan`, so there is no interpreter start left to hang."""
    hung = """#!/bin/bash
case "$*" in
{run}
  *deploy_locks.py*) sleep 30 ;;
  *) exit 0 ;;
esac
""".replace("{run}", UV_DEPLOY_RUN_ARM)
    result, service_flocks = _run_detach(
        tmp_path, hung, "--tags", "alpha", HOMELAB_DEPLOY_LOCK_PLAN_TIMEOUT="1"
    )
    assert result.returncode == DEPLOY_LOCK_PLAN_FAILED, (result.stdout, result.stderr)
    assert service_flocks == []
    assert "longer than 1s" in result.stderr


# -- what the shell no longer carries ------------------------------------------------------


def _code_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_deploy_sh_neither_names_a_service_lock_nor_sorts_a_tag_list():
    """The verify line of issue #2054: only comments may say `server-deploy-`, and no `sort`
    of a tag list -- with or without `LC_ALL` -- is left for a locale to disagree with."""
    code = _code_lines(_DEPLOY_LOCKED.read_text())
    naming = [line for line in code if "server-deploy-" in line]
    assert naming == [], f"deploy.sh still names a service lock itself: {naming}"
    sorting = [line for line in code if "LC_ALL" in line or "sort -u" in line]
    assert sorting == [], f"deploy.sh still sorts a tag list itself: {sorting}"


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


def test_deploy_sh_default_tree_lock_is_the_module_constant():
    """The one lock the shell still names by literal -- it takes it before any Python runs --
    pinned to the constant every Python reader imports."""
    line = next(
        line
        for line in _code_lines(_DEPLOY_LOCKED.read_text())
        if line.startswith("LOCK=")
    )
    assert deploy_locks.TREE_LOCK in line, (
        f"deploy_locked.sh's tree lock ({line}) is not deploy_locks.TREE_LOCK "
        f"({deploy_locks.TREE_LOCK}); the wrapper and the deployer would guard different files"
    )
