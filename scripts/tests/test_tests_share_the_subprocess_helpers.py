"""A test writes its fake binaries and its `PATH` through `lib.proc_testing`, never by hand.

`lib.proc_testing` exists because every test that drives a shell script re-decided the same
three things (#3056): the launch flags, the `PATH` prefix and the exec bit. The 2026-09-28
tests audit counted 64 local `_run`/`run_script` wrappers and 11 fake-binary writers, and they
disagreed on all three. A writer that forgets the exec bit fails as `Permission denied` from
inside the script under test, which reads as a bug in the script rather than in the fixture.

This refuses the next one. It is the sibling of
`scripts/tests/test_scratch_repos_go_through_git_testing.py`, which holds the same line for a
scratch git repository, and of `test_tests_share_render_and_path_helpers.py` for the Jinja
environment and the repo root.

THREE RULES, each with a clean/flagged pair below.

1. A test module does not write an executable file by hand. The AST shape is a `.chmod(<mode
   carrying an exec bit>)` on a path the same function also wrote, which is a fake binary; a
   `chmod` on a path the function did not write is a permission fixture (`0o500` on a directory,
   to prove an unreadable state reads as a fault) and is not a hit.
2. A test module does not build a `PATH` prefix by hand. The shape is a string that puts
   something in front of an existing `PATH` value, whether it lands in an `env=` dict or in
   `monkeypatch.setenv`. `lib.proc_testing.path_with` is that string, and
   `run(..., stub_bin=...)` is the call that never spells it.
3. A test module does not launch a subprocess with no deadline. The shape is a call to
   `subprocess.run`/`check_output`/`check_call`/`call` that passes no `timeout=`, or a
   `from subprocess import run` that would let the sanctioned bare `run(` name the unbounded
   one. `lib.proc_testing.run` supplies `DEFAULT_TIMEOUT`, so a call through it is clean,
   and so is an explicit `timeout=None` where outliving every deadline is the point — which is
   why rule 3 exempts no module, unlike the two above it. `subprocess.Popen` is NOT covered: it
   takes no `timeout=` at all, and its deadline lives on the `wait`/`communicate` that follows,
   which is a different AST shape with its own exemptions (#3073).

Every rule reads the AST, so a docstring naming the old form is prose rather than a hit.

Run: uv run pytest scripts/tests/test_tests_share_the_subprocess_helpers.py
"""

import ast
from pathlib import Path

from lib.repo_paths import ANSIBLE, REPO, SCRIPTS

# Every `testpaths` entry that holds Python, so the rule covers what pytest collects. All of
# them can import `lib.proc_testing`: `pyproject.toml` puts `scripts/` on `pythonpath` for the
# whole session, so depth does not matter.
_TEST_ROOTS = (
    SCRIPTS,
    ANSIBLE / "tests",
    ANSIBLE / "roles",
    REPO / ".claude" / "hooks",
    REPO / ".claude" / "tests",
    REPO / "evals",
)

# Modules whose SUBJECT is one of the three decisions, or whose environment is deliberately
# not the real one. Each keeps its raw form and says why here.
EXEMPT = {
    # This module: both rules are held here as fixture text, which is what a red-proof pair is.
    "scripts/tests/test_tests_share_the_subprocess_helpers.py",
    # The helpers themselves. `test_proc_testing.py`'s subject is the exec bit and the prefix.
    "scripts/lib/tests/test_proc_testing.py",
    # The exec bit IS the subject: `hook_files` must read a hook committed WITHOUT it (#361),
    # so the fixture writes one file with the bit and one without.
    "scripts/dev/tests/test_gen_hook_settings.py",
}

# RULE 3 HAS NO EXEMPTION SET, and that is deliberate. Rules 1 and 2 need one because their
# subject — the exec bit, the `PATH` prefix — cannot be written any other way. Rule 3's can: a
# launch that must outlive every deadline passes `timeout=None`, which satisfies the rule and
# puts the decision at the line that made it. A module-wide exemption would blanket that
# module's OTHER launches too, which is the cost rules 1 and 2 accept and rule 3 need not.
# #3066 predicted one entry here, `ansible/tests/deploy/test_gitops_deploy_subprocess.py`; no
# such file exists, and no site in the 2026-10-01 census needed an unbounded launch.

# The rules' own census must reach these. Each held one of the two forms before #3056, so an
# empty or partial scan means the walk stopped matching rather than that the tree is clean.
KNOWN_MEMBERS = frozenset(
    {
        ".claude/hooks/tests/conftest.py",
        "ansible/tests/setup/test_github_ruleset_drift.py",
        "ansible/tests/staging/_fence_probe.py",
        "scripts/deploy_tools/tests/test_land_detach.py",
        "scripts/tests/test_ansible_lint_hook.py",
        "scripts/validate/tests/test_vale_sync_guard.py",
    }
)

# Rule 3's census. Each of these launched without a deadline before this guard landed, and
# they span the three roots that held one — the hooks tree, `ansible/tests` and `scripts` — so
# a scan that stops reaching them reads as a clean tree.
KNOWN_UNBOUNDED_MEMBERS = frozenset(
    {
        ".claude/hooks/tests/test_hook_scripts_executable.py",
        "ansible/tests/_helpers.py",
        "ansible/tests/repo/test_testpaths_covers_every_test_file.py",
        "scripts/deploy_tools/tests/test_land_tools.py",
        "scripts/lib/tests/test_git.py",
    }
)

# The launches that take a `timeout=`. `Popen` is deliberately absent — see rule 3 above.
_BOUNDABLE_LAUNCHES = frozenset({"run", "check_output", "check_call", "call"})

# The module names a `subprocess` import can be bound to. A hit needs the call to reach the
# stdlib module rather than any object that happens to own a `run`, and `sp` is the one alias
# this tree uses.
_SUBPROCESS_NAMES = frozenset({"subprocess", "sp"})

_EXEC_BITS = 0o111


def _is_test_module(rel: Path) -> bool:
    """A module pytest collects or a test imports: `test_*.py`, `conftest.py` or `_*.py`."""
    return "tests" in rel.parts and (
        rel.name.startswith(("test_", "_")) or rel.name == "conftest.py"
    )


def _test_modules() -> list[Path]:
    found = []
    for root in _TEST_ROOTS:
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO)
            if "collections" not in rel.parts and _is_test_module(rel):
                found.append(path)
    return sorted(set(found))


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _join_base(node: ast.expr) -> str | None:
    """The leftmost operand of a `dir / "name"` path join, or None if `node` is not one.

    `(bin_dir / stub).chmod(0o755)` in a loop names a different expression from the
    `(bin_dir / "flock").write_text(...)` that wrote it, so the base is what pairs them.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _join_base(node.left) or ast.unparse(node.left)
    return None


def handwritten_executables(source: str) -> list[str]:
    """The paths `source` writes and then chmods executable, by the expression it names them by.

    A `chmod` alone is a permission fixture — `0o500` on a state directory, to prove an
    unreadable state reads as a fault rather than as a pass — and is not a hit. The pair is
    what makes it a fake binary, so the chmodded path must also be written in the same
    function, either as the same expression or as a join under the same directory.
    """
    found = []
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            continue
        writes = [
            node.func.value
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"write_text", "write_bytes"}
        ]
        written = {ast.unparse(node) for node in writes}
        written_dirs = {base for node in writes if (base := _join_base(node))}
        for node in ast.walk(scope):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chmod"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, int)
                and node.args[0].value & _EXEC_BITS
            ):
                continue
            target = node.func.value
            base = _join_base(target)
            if ast.unparse(target) in written or (base and base in written_dirs):
                found.append(ast.unparse(target))
    return sorted(set(found))


def handbuilt_path_prefixes(source: str) -> list[str]:
    """The `PATH` prefixes `source` builds itself, by the expression each is built from.

    A hit is a string that puts something in FRONT of an existing `PATH` value. The two
    spellings in the tree are an f-string (`f"{stub}{os.pathsep}{os.environ['PATH']}"`) and a
    concatenation; both read as a JoinedStr or a BinOp whose text mentions a `PATH` read.
    A literal `PATH` value that names no existing one — a fixture pinning `/usr/bin:/bin` — is
    the subject's own environment rather than a prefix, and is not a hit.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr | ast.BinOp):
            continue
        text = ast.unparse(node)
        if "PATH" not in text:
            continue
        # A read of an existing PATH is what makes it a prefix rather than a fresh value.
        if not any(
            read in text
            for read in ("os.environ['PATH']", '"PATH"]', "['PATH']", ".get('PATH'")
        ):
            continue
        found.append(text)
    return sorted(set(found))


def test_the_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_every_exemption_still_names_a_file_that_exists():
    """A stale exemption silently lets a whole module back out of both rules."""
    gone = sorted(name for name in EXEMPT if not (REPO / name).exists())
    assert gone == [], f"exempted modules that no longer exist: {gone}"


def test_no_test_module_writes_an_executable_by_hand():
    offenders = {
        _rel(p): names
        for p in _test_modules()
        if _rel(p) not in EXEMPT and (names := handwritten_executables(p.read_text()))
    }
    assert offenders == {}, (
        "these tests write a fake binary by hand, re-deciding the shebang and the exec bit. "
        "Use `lib.proc_testing.fake_bin(directory, name=body)` for a stub directory, or "
        f"`write_exec(path, body)` for a single script: {offenders}"
    )


def test_no_test_module_builds_a_path_prefix_by_hand():
    offenders = {
        _rel(p): names
        for p in _test_modules()
        if _rel(p) not in EXEMPT and (names := handbuilt_path_prefixes(p.read_text()))
    }
    assert offenders == {}, (
        "these tests put a stub directory in front of PATH by hand. Pass the directory as "
        "`lib.proc_testing.run(..., stub_bin=...)`, or use `path_with(directory, env=...)` "
        f"where the environment goes somewhere else: {offenders}"
    )


def test_a_handwritten_executable_is_flagged_in_each_spelling():
    assert handwritten_executables(
        "def f(p):\n    s = p / 'git'\n    s.write_text('#!/bin/sh\\n')\n    s.chmod(0o755)\n"
    ) == ["s"]
    assert handwritten_executables(
        "def f(p):\n    (p / 'git').write_text('x')\n    (p / 'git').chmod(0o700)\n"
    ) == ["p / 'git'"]
    # The loop spelling: the chmod names a different expression from the write, so the
    # directory they share is what pairs them.
    assert handwritten_executables(
        "def f(p):\n"
        "    (p / 'git').write_text('x')\n"
        "    for n in ('git',):\n        (p / n).chmod(0o755)\n"
    ) == ["p / n"]


def test_a_mode_restore_on_a_directory_beside_a_fake_bin_is_not_flagged():
    """`root.chmod(0o755)` undoes an unwritable-directory fixture; `root` is not a binary."""
    assert (
        handwritten_executables(
            "def f(p):\n    (root / 'hold').write_text('x')\n    root.chmod(0o755)\n"
        )
        == []
    )


def test_a_permission_fixture_is_not_flagged():
    """`0o500` on a directory the test never wrote proves an unreadable state is a fault."""
    assert handwritten_executables("def f(p):\n    p.parent.chmod(0o500)\n") == []
    assert (
        handwritten_executables(
            "def f(p):\n    p.write_text('x')\n    p.chmod(0o000)\n"
        )
        == []
    )
    assert (
        handwritten_executables(
            '"""s.write_text(t); s.chmod(0o755) used to be here."""\n'
        )
        == []
    )


def test_a_handbuilt_path_prefix_is_flagged_in_each_spelling():
    assert (
        handbuilt_path_prefixes(
            "env = {'PATH': f\"{d}{os.pathsep}{os.environ['PATH']}\"}\n"
        )
        != []
    )
    assert handbuilt_path_prefixes("env['PATH'] = str(d) + ':' + env['PATH']\n") != []
    assert (
        handbuilt_path_prefixes(
            "monkeypatch.setenv('PATH', f\"{d}:{os.environ['PATH']}\")\n"
        )
        != []
    )


def test_a_pinned_path_value_is_not_flagged():
    """A fixture holding its subject to `/usr/bin:/bin` sets the environment, not a prefix."""
    assert handbuilt_path_prefixes("env = {'PATH': '/usr/bin:/bin'}\n") == []
    assert handbuilt_path_prefixes("env = {'PATH': f'{d}:/usr/bin'}\n") == []


def test_the_shared_helpers_are_clean():
    clean = (
        "from lib.proc_testing import fake_bin, run\n"
        "def f(tmp_path):\n"
        "    stubs = fake_bin(tmp_path / 'bin', git='echo x\\n')\n"
        "    return run(['bash', 's.sh'], stub_bin=stubs)\n"
    )
    assert handwritten_executables(clean) == []
    assert handbuilt_path_prefixes(clean) == []


def unbounded_launches(source: str) -> list[str]:
    """The subprocess launches `source` makes with no deadline, by the text of each.

    A hit is one of two shapes. The first is a call to `subprocess.run` (or `check_output`,
    `check_call`, `call`) carrying no `timeout=` keyword: the child can wedge, and nothing
    above it ever gives up, so the symptom is a CI run that never ends rather than a failing
    test. A `**kwargs` splat counts as bounded, because the keyword can be in it and the AST
    cannot say.

    The second is `from subprocess import run`, which would bind the unbounded launch to the
    bare `run` name that `lib.proc_testing.run` is imported under. A test reading
    `run(["bash", script])` would then have no way to tell which one it got.

    `subprocess.Popen` is not a hit: it accepts no `timeout=`, so there is no bounded spelling
    of it to demand here.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            found += [
                f"from subprocess import {alias.name}"
                for alias in node.names
                if alias.name in _BOUNDABLE_LAUNCHES
            ]
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _BOUNDABLE_LAUNCHES):
            continue
        if ast.unparse(func.value).split(".")[-1] not in _SUBPROCESS_NAMES:
            continue
        # `**kwargs` can carry the timeout, and `arg is None` is how the AST spells it.
        if any(kw.arg in ("timeout", None) for kw in node.keywords):
            continue
        found.append(ast.unparse(node)[:120])
    return sorted(set(found))


def test_the_unbounded_census_reaches_every_migrated_module():
    found = {_rel(p) for p in _test_modules()}
    missing = KNOWN_UNBOUNDED_MEMBERS - found
    assert not missing, f"the scan no longer reaches: {sorted(missing)}"


def test_no_test_module_launches_a_subprocess_without_a_deadline():
    offenders = {
        _rel(p): names
        for p in _test_modules()
        if (names := unbounded_launches(p.read_text()))
    }
    assert offenders == {}, (
        "these tests launch a child with no deadline, so a wedged one parks the whole CI run "
        "instead of failing its own test. Call `lib.proc_testing.run(...)`, which supplies "
        "`DEFAULT_TIMEOUT`, or pass your own `timeout=` with the reason at the line — "
        f"`timeout=None` included, where outliving every deadline is the point: {offenders}"
    )


def test_an_unbounded_launch_is_flagged_in_each_spelling():
    assert unbounded_launches("subprocess.run(['x'], check=True)\n") != []
    assert unbounded_launches("subprocess.check_output(['x'])\n") != []
    assert unbounded_launches("sp.call(['x'])\n") != []
    assert unbounded_launches("import subprocess\nfrom subprocess import run\n") != []


def test_a_bounded_launch_is_not_flagged():
    assert unbounded_launches("subprocess.run(['x'], timeout=5)\n") == []
    assert unbounded_launches("subprocess.run(['x'], **kw)\n") == []
    # `Popen` has no `timeout=`, so rule 3 cannot ask for one. #3073 holds the remainder.
    assert unbounded_launches("subprocess.Popen(['x'])\n") == []
    # The helper's own spelling, and a `run` that is not the stdlib's.
    assert unbounded_launches("run(['x'])\nclient.run(['x'])\n") == []
    # A docstring naming the raw form is prose.
    assert unbounded_launches('"""subprocess.run([x]) used to be here."""\n') == []
