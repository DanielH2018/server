"""Both deploy paths must invoke `ansible-playbook` the way ADR-0017 says.

Two invariants, one per path, and each is invisible at run time — a deploy that renders from
the working tree instead of the snapshot, or one that skips its service lock, works perfectly
until something else deploys at the same moment. Neither failure has a symptom on the run that
caused it, which is why they are pinned at the source.

`scripts/deploy.sh`: a foreground run's playbook is `run_playbook` in `deploy_playbook.py`,
which runs it with the snapshot as its cwd, called by `deploy_under_locks.run` after
`take_service_locks`. `--detach` still goes through `run_playbook_in_snapshot` in
`deploy_locked.sh`, which cds into the snapshot. `--check` and `--dry-run` are the
exceptions: `deploy_run.py` execs them above the lock, from the working tree on purpose,
and names ansible-playbook nowhere else.

`deploy_io.py`: every function that runs a playbook wraps it in `service_locks`. The set of
such functions is asserted by name, so one added later without a lock fails here rather than
passing an `all(...)` over a set it was never in.

Run: uv run pytest ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py
"""

import ast
import re

from _helpers import REPO

# The locked half behind the deploy.sh shim, and the Python front half that execs it.
_DEPLOY_SH = REPO / "scripts/deploy_tools/deploy_locked.sh"
_DEPLOY_RUN = REPO / "scripts/deploy_tools/deploy_run.py"
_DEPLOY_UNDER_LOCKS = REPO / "scripts/deploy_tools/deploy_under_locks.py"
_DEPLOY_PLAYBOOK = REPO / "scripts/deploy_tools/deploy_playbook.py"
_DEPLOY_IO = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_io.py"
_DEPLOY_LOCKS = REPO / "ansible/roles/setup/gitops_deploy/files/deploy_locks.py"

_SNAPSHOT_RUNNER = "run_playbook_in_snapshot"
# The deployer's playbook call sites. Named rather than discovered so that a fourth one added
# without a lock fails this file instead of joining a vacuously-true census.
_LOCKED_DEPLOY_FUNCTIONS = frozenset({"deploy", "deploy_k8s", "deploy_broad"})
# The helper that shares one deadline between the lock wait and the run, and the two call sites
# that carry a phase budget to share. `deploy` is out because its run is unbounded.
_BUDGET_HELPER = "locked_budget"
_LOCK_HELPERS = frozenset({"service_locks", _BUDGET_HELPER})
_BUDGETED_DEPLOY_FUNCTIONS = frozenset({"deploy_k8s", "deploy_broad"})


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(1-indexed line number, text) for every line that is not a comment or blank."""
    return [
        (i, line)
        for i, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _runner_line_range(text: str) -> tuple[int, int]:
    """The 1-indexed first and last line of `run_playbook_in_snapshot`'s definition."""
    lines = text.splitlines()
    first = next(
        i
        for i, line in enumerate(lines, start=1)
        if line.startswith(f"{_SNAPSHOT_RUNNER}()")
    )
    last = next(
        i for i, line in enumerate(lines[first:], start=first + 1) if line == "}"
    )
    return first, last


def test_the_locked_half_names_ansible_playbook_only_in_the_runner():
    """A locked invocation that bypasses the runner fails here."""
    text = _DEPLOY_SH.read_text()
    first, last = _runner_line_range(text)
    naming = [
        (n, line.strip())
        for n, line in _code_lines(text)
        if "ansible-playbook" in line
        # The runner's own invocation is the compliant one, and an error message naming the
        # command is prose rather than a call site.
        and not (first <= n <= last)
        and not line.lstrip().startswith("echo ")
    ]
    assert naming == [], (
        f"{naming} run ansible-playbook outside {_SNAPSHOT_RUNNER}; a locked deploy must "
        "render from the snapshot, not the working tree"
    )


def _unlocked_playbook_calls(source: str) -> list[str]:
    """The test of the `if` enclosing each "ansible-playbook" literal in `source`.

    An empty string stands for a literal no `if` encloses.
    """
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "ansible-playbook":
            guard, cur = "", node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.If):
                    guard = ast.unparse(cur.test)
                    break
            found.append(guard)
    return found


def test_deploy_run_execs_ansible_playbook_only_for_check_and_dry_run():
    """The front half runs no playbook of its own except the two unlocked modes.

    The count is asserted as well as the guard: a rewrite that dropped the exec would
    otherwise leave nothing to check and read green.
    """
    assert _unlocked_playbook_calls(_DEPLOY_RUN.read_text()) == [
        "plan.check or plan.dry_run"
    ]


def test_an_unguarded_playbook_call_is_flagged():
    assert _unlocked_playbook_calls(
        'def run(plan):\n    exec_argv(["uv", "run", "ansible-playbook"])\n'
    ) == [""]


def test_the_snapshot_runner_is_the_only_locked_path_and_it_cds_into_the_snapshot():
    """The runner has to actually enter the snapshot, and the --detach arm has to use it."""
    text = _DEPLOY_SH.read_text()
    body = text.split(f"{_SNAPSHOT_RUNNER}() {{", 1)
    assert len(body) == 2, f"{_SNAPSHOT_RUNNER} is gone from {_DEPLOY_SH.name}"
    definition = body[1].split("\n}", 1)[0]
    assert 'cd "$snapshot"' in definition, (
        f"{_SNAPSHOT_RUNNER} no longer changes into the snapshot, so a locked deploy renders "
        "from whatever tree the wrapper happens to be sitting in"
    )
    assert "UV_PROJECT_ENVIRONMENT=" in definition, (
        f"{_SNAPSHOT_RUNNER} must pin the caller's venv; a snapshot has none and uv would "
        "build one in a directory this run deletes"
    )
    calls = [n for n, line in _code_lines(text) if f"{_SNAPSHOT_RUNNER} " in line]
    assert len(calls) == 1, (
        f"expected the --detach arm alone to call {_SNAPSHOT_RUNNER} (the foreground runs in "
        f"deploy_under_locks.py since #2412 slice 3), found {len(calls)} call sites"
    )


def _playbook_literal_owners(source: str) -> list[str]:
    """The enclosing function of each "ansible-playbook" constant in `source`, in order."""
    tree = ast.parse(source)
    return [
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and node.value == "ansible-playbook"
    ]


def test_the_python_locked_half_runs_ansible_playbook_only_in_run_playbook():
    """One call site, and it runs from the snapshot on the calling checkout's venv."""
    source = _DEPLOY_PLAYBOOK.read_text()
    assert _playbook_literal_owners(source) == ["run_playbook"]
    assert _playbook_literal_owners(_DEPLOY_UNDER_LOCKS.read_text()) == []
    runner = ast.unparse(_functions(source)["run_playbook"])
    assert "cwd=run.snapshot" in runner, (
        "run_playbook no longer runs from the snapshot, so a locked deploy renders from "
        "whatever tree the wrapper happens to be sitting in"
    )
    assert "UV_PROJECT_ENVIRONMENT" in runner, (
        "run_playbook must pin the caller's venv; a snapshot has none and uv would build "
        "one in a directory this run deletes"
    )


def test_a_second_playbook_call_site_is_flagged():
    assert _playbook_literal_owners(
        "def run_playbook():\n    x = 'ansible-playbook'\n"
        "def run():\n    y = 'ansible-playbook'\n"
    ) == ["run_playbook", "run"]


def _call_order(function: ast.FunctionDef) -> list[str]:
    """The names of the plain calls in `function`, in source order."""
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    return [
        call.func.id
        for call in sorted(calls, key=lambda c: (c.lineno, c.col_offset))
        if isinstance(call.func, ast.Name)
    ]


def _locks_before_playbook(function: ast.FunctionDef) -> bool:
    order = _call_order(function)
    if "take_service_locks" not in order or "run_playbook" not in order:
        return False
    return order.index("take_service_locks") < order.index("run_playbook")


def test_the_python_locked_half_takes_its_service_locks_before_the_playbook():
    """The ordering ADR-0017 fixes, read off `run` in deploy_under_locks.py."""
    run = _functions(_DEPLOY_UNDER_LOCKS.read_text())["run"]
    assert _locks_before_playbook(run), (
        "deploy_under_locks.run reaches the playbook before it takes a service lock"
    )


def test_a_playbook_before_its_locks_is_flagged():
    assert not _locks_before_playbook(
        _one_function("def run(s):\n    run_playbook(s)\n    take_service_locks(s)\n")
    )


def test_deploy_sh_takes_a_service_lock_before_it_runs_anything():
    """The ordering ADR-0017 fixes, read off the file: locks first, then the playbook."""
    text = _DEPLOY_SH.read_text()
    first_lock = min(n for n, line in _code_lines(text) if "take_service_locks" in line)
    first_run = min(
        n for n, line in _code_lines(text) if f"{_SNAPSHOT_RUNNER} " in line
    )
    assert first_lock < first_run, (
        f"{_DEPLOY_SH.name} reaches the playbook before it takes a service lock"
    )


def _one_function(source: str) -> ast.FunctionDef:
    """The single function a snippet defines, for the red proofs below."""
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _functions(source: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(source)
    return {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }


def _runs_a_playbook(node: ast.FunctionDef) -> bool:
    """Does this function build an argv that runs a playbook?

    The shared prefix `PLAYBOOK_ARGV` by name, or the literal `ansible-playbook` for a call
    site that spells its own argv. Matched as an exact constant rather than a substring of the
    source: `run`'s own docstring explains what happens to an `ansible-playbook` grandchild,
    and a substring test would count that prose as a call site.
    """
    return any(
        (isinstance(inner, ast.Constant) and inner.value == "ansible-playbook")
        or (isinstance(inner, ast.Name) and inner.id == "PLAYBOOK_ARGV")
        for inner in ast.walk(node)
    )


def _guarded_by_service_locks(node: ast.FunctionDef) -> bool:
    for inner in ast.walk(node):
        if not isinstance(inner, ast.With):
            continue
        for item in inner.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and getattr(call.func, "id", "") in _LOCK_HELPERS
            ):
                return True
    return False


def _shares_one_budget(node: ast.FunctionDef) -> bool:
    """Does every `run` in this function take the budget `locked_budget` yielded?

    `locked_budget(services, timeout) as budget` then `run(..., timeout=budget)`. Passing the
    phase's own `timeout` to both is the shape this rejects: it gives the wait a second budget
    the size of the first, and a phase that waits then runs holds the git-tree lock for twice
    what `_worst_lock_hold()` says it can.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.With):
            continue
        for item in inner.items:
            call = item.context_expr
            if not (
                isinstance(call, ast.Call)
                and getattr(call.func, "id", "") == _BUDGET_HELPER
            ):
                continue
            if not isinstance(item.optional_vars, ast.Name):
                return False
            budget = item.optional_vars.id
            runs = [
                c
                for c in ast.walk(inner)
                if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "run"
            ]
            return bool(runs) and all(
                any(
                    kw.arg == "timeout"
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == budget
                    for kw in c.keywords
                )
                for c in runs
            )
    return False


def test_every_deployer_playbook_call_site_is_the_named_set():
    """Non-vacuity for the assertion below: the census must find the functions it checks."""
    functions = _functions(_DEPLOY_IO.read_text())
    found = {name for name, node in functions.items() if _runs_a_playbook(node)}
    assert found == _LOCKED_DEPLOY_FUNCTIONS, (
        f"deploy_io.py's playbook call sites are {sorted(found)}, not "
        f"{sorted(_LOCKED_DEPLOY_FUNCTIONS)} — a new one must take its service locks too"
    )


def test_every_deployer_playbook_call_site_holds_its_service_locks():
    """The invariant itself. An unlocked deploy races another deploy of the same service."""
    functions = _functions(_DEPLOY_IO.read_text())
    unguarded = [
        name
        for name in sorted(_LOCKED_DEPLOY_FUNCTIONS)
        if not _guarded_by_service_locks(functions[name])
    ]
    assert not unguarded, (
        f"{unguarded} run ansible-playbook without `with service_locks(...)`"
    )


def test_a_budgeted_deploy_shares_one_deadline_between_its_wait_and_its_run():
    """CLEAN half: the tree-lock hold a phase's timeout is allowed to buy, held to once.

    `gitops-deploy.service` holds `/var/lock/server-git-tree.lock` across its whole run, so a
    phase that waits for a service lock is holding the tree lock while it waits.
    `_worst_lock_hold()` in tests/test_gitops_deploy_timeout_budgets.py sums the phase timeouts
    as that hold, and the four jobs waiting on the tree lock size their own waits from that sum.
    A wait budgeted separately from the run breaks the sum with every check still green.
    """
    functions = _functions(_DEPLOY_IO.read_text())
    unshared = [
        name
        for name in sorted(_BUDGETED_DEPLOY_FUNCTIONS)
        if not _shares_one_budget(functions[name])
    ]
    assert not unshared, (
        f"{unshared} do not pass the budget `{_BUDGET_HELPER}` yielded to their `run`, so the "
        "lock wait and the playbook each get the phase's whole timeout"
    )


def test_a_separately_budgeted_wait_is_flagged():
    """FLAGGED half for the guard above, which can only ever be observed passing.

    This is the pre-fix shape: the same `timeout` handed to the lock helper and to `run`.
    """
    before = _one_function(
        "def deploy_k8s(repo, services, timeout):\n"
        "    with service_locks(services, timeout):\n"
        "        run(argv, cwd=repo, timeout=timeout)\n"
    )
    after = _one_function(
        "def deploy_k8s(repo, services, timeout):\n"
        "    with locked_budget(services, timeout) as budget:\n"
        "        run(argv, cwd=repo, timeout=budget)\n"
    )
    assert not _shares_one_budget(before), (
        "the guard accepts a wait that is budgeted separately from the run; it is measuring "
        "nothing"
    )
    assert _shares_one_budget(after)


def test_the_service_lock_helper_takes_the_all_lock_before_any_service():
    """The ordering both sides depend on, asserted where the deployer fixes it.

    `all` first, then sorted names, and `service_locks` walks `plan` rather than restating
    it -- `deploy.sh` reads the same `plan` off the CLI, so a second statement of the order
    anywhere is what issue #2054 removed. Reversing either half reintroduces a lock cycle
    between a full run and a scoped one.
    """
    source = _DEPLOY_LOCKS.read_text()
    body = ast.unparse(_functions(source)["plan"])
    all_at = body.index("SERVICE_LOCK_ALL")
    tags_at = body.index("for name in names")
    assert all_at < tags_at, "plan no longer puts the `all` lock first"
    assert re.search(r"names\s*=\s*sorted\(set\(services\)\)", body), (
        "plan no longer orders the per-service locks by code point"
    )
    taker = ast.unparse(_functions(source)["service_locks"])
    assert "in plan(" in taker and "sorted(" not in taker, (
        "service_locks orders the locks itself instead of walking plan()"
    )
