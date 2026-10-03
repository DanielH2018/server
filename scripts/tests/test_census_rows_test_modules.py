"""Censuses of the pytest-only modules: the git and subprocess shared-helper rules, one row each.

A test module does not re-derive what a shared helper already decides: its scratch git
repository (`lib.git_testing`), or its fake binaries, `PATH` and deadline (`lib.proc_testing`).
The Jinja environment, repo root and shell-template rules are the same shape, in
`test_census_rows_test_renders.py`. Each rule was a test in one of three files of its own until
#3430. The detectors are in `_test_module_rules.py`; a row's `reason` keeps what its file's
docstring said a reader needs before changing the rule.

Every row in both files reads one census, `pytest_only_modules()`: each tracked module
`is_pytest_only` accepts under a `testpaths` root. The three files each had their own walk; this is the render-and-path file's,
the widest of them. It drops none of the other two's modules. It adds `evals/tests/` and
`scripts/conftest.py` to the git rules, and `scripts/conftest.py`, `ansible/tests/leakguard.py`
and home-assistant's `jinja_harness.py` to the proc_testing rules.

Run: uv run pytest scripts/tests/test_census_rows_test_modules.py
"""

import pytest
from _row_table import (
    Census,
    Subject,
    check,
    lines_matching,
    proof_problems,
)
from _test_module_rules import (
    GIT_SCRUB,
    MODULE_FLOOR,
    handbuilt_path_prefixes,
    handwritten_executables,
    pytest_only_modules,
    raw_git_calls,
    unbounded_launches,
)

SELF = "scripts/tests/test_census_rows_test_modules.py"

ROWS = (
    Census(
        name="tests-run-no-git-write-through-subprocess",
        reason=(
            "`lib.git_testing` exists because twenty-five test modules each re-derived the same "
            "`GIT_*` scrub and disagreed on what went in it. A module that forgets one line "
            "passes on a workstation and writes the primary repository under `prek`'s pytest "
            "hook, where `git commit` exports `GIT_DIR` and `GIT_INDEX_FILE`. A read verb "
            "against the real checkout (`git ls-files`) is harmless; a write verb goes through "
            "`lib.git_testing.git`. Read from the AST, so a docstring is prose."
        ),
        files=pytest_only_modules,
        offence=lambda s: raw_git_calls(s.text),
        red=(
            Subject("a.py", 'subprocess.run(["git", "init"], cwd=p)'),
            Subject("b.py", 'subprocess.run(("git", "commit"), cwd=p)'),
            Subject("c.py", 'subprocess.check_output(["git", "worktree", "add", p])'),
        ),
        green=(
            Subject("a.py", 'from lib.git_testing import git\ngit(repo, "status")\n'),
            Subject("b.py", 'subprocess.run(["git", "ls-files", "-z"], cwd=REPO)'),
            Subject("c.py", 'subprocess.run(["git", "rev-parse", "HEAD"])'),
            Subject("d.py", 'subprocess.run(("git", "worktree", "list"))'),
            Subject("e.py", 'subprocess.run(["bash", str(script)], cwd=p)'),
            Subject("f.py", "subprocess.run([sys.executable, str(tool)])"),
            Subject(
                "g.py", 'subprocess.CompletedProcess(args=["git", "x"], returncode=0)'
            ),
            Subject("h.py", '"""A raw subprocess.run(["git", ...]) here wrote it."""'),
        ),
        min_matches=MODULE_FLOOR,
        must_find=frozenset(
            {
                "ansible/tests/_ci_scoping.py",
                "ansible/tests/longhorn/test_volume_snapshot_register.py",
                "ansible/tests/repo/test_facts_lock_matches_tree.py",
                "ansible/roles/setup/renovate_agent/tests/test_prepare_worktree.py",
                "scripts/deploy_tools/tests/_narrow_fixtures.py",
                "scripts/deploy_tools/tests/test_deploy_staleness.py",
                "scripts/deploy_tools/tests/test_land_tools.py",
                "scripts/dev/tests/test_prune_worktrees.py",
                "scripts/diagnostics/tests/_release_fixtures.py",
                "scripts/lib/tests/test_facts_lock.py",
                "scripts/lib/tests/test_render_guard.py",
            }
        ),
    ),
    Census(
        name="tests-take-the-git-scrub-from-git-testing",
        reason=(
            "The other half of the git rule: a test module does not re-derive the `GIT_*` "
            "scrub as its own comprehension. Import `scrubbed_env` from `lib.git_testing`."
        ),
        files=pytest_only_modules,
        offence=lines_matching(GIT_SCRUB),
        red=(
            Subject(
                "a.py",
                'env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}',
            ),
            Subject(
                "b.py", "for v in [n for n in os.environ if n.startswith('GIT_')]:"
            ),
        ),
        green=(Subject("a.py", "from lib.git_testing import scrubbed_env\n"),),
        min_matches=MODULE_FLOOR,
        allow={
            SELF: "the red fixtures above hold the offending spelling as text",
            "scripts/lib/tests/test_git_testing.py": "the module under test IS the scrub",
            "scripts/deploy_tools/tests/_deploy_sh_fakes.py": (
                "`git_free_env` builds the whole environment a `deploy.sh` child runs under, "
                "the shared scrub WITHOUT the scratch commit identity, as its docstring says"
            ),
        },
    ),
    Census(
        name="tests-write-fake-binaries-through-proc-testing",
        reason=(
            "`lib.proc_testing` exists because every test driving a shell script re-decided "
            "the launch flags, the `PATH` prefix and the exec bit (#3056). A writer that "
            "forgets the exec bit fails as `Permission denied` from inside the script under "
            "test, which reads as a bug in the script. The shape is a `.chmod(<exec bit>)` on "
            "a path the same function wrote; a chmod alone is a permission fixture. Use "
            "`fake_bin(directory, name=body)` or `write_exec(path, body)`."
        ),
        files=pytest_only_modules,
        offence=lambda s: handwritten_executables(s.text),
        red=(
            Subject(
                "a.py",
                "def f(p):\n    s = p / 'git'\n    s.write_text('#!/bin/sh\\n')\n"
                "    s.chmod(0o755)\n",
            ),
            Subject(
                "b.py",
                "def f(p):\n    (p / 'git').write_text('x')\n    (p / 'git').chmod(0o700)\n",
            ),
            # The loop spelling: the directory the write and the chmod share pairs them.
            Subject(
                "c.py",
                "def f(p):\n    (p / 'git').write_text('x')\n"
                "    for n in ('git',):\n        (p / n).chmod(0o755)\n",
            ),
        ),
        green=(
            Subject(
                "a.py",
                "def f(p):\n    (root / 'hold').write_text('x')\n    root.chmod(0o755)\n",
            ),
            Subject("b.py", "def f(p):\n    p.parent.chmod(0o500)\n"),
            Subject("c.py", "def f(p):\n    p.write_text('x')\n    p.chmod(0o000)\n"),
            Subject("d.py", '"""s.write_text(t); s.chmod(0o755) used to be here."""\n'),
            Subject(
                "e.py",
                "from lib.proc_testing import fake_bin\n"
                "def f(tmp_path):\n    fake_bin(tmp_path / 'bin', git='echo x\\n')\n",
            ),
        ),
        min_matches=MODULE_FLOOR,
        must_find=frozenset(
            {
                ".claude/hooks/tests/conftest.py",
                "ansible/tests/setup/test_github_ruleset_drift.py",
                "ansible/tests/staging/_fence_probe.py",
                "scripts/deploy_tools/tests/test_land_detach.py",
                "scripts/tests/test_ansible_lint_hook.py",
                "scripts/validate/tests/test_vale_sync_guard.py",
            }
        ),
        allow={
            "scripts/dev/tests/test_gen_hook_settings.py": (
                "the exec bit IS the subject: `hook_files` must read a hook committed WITHOUT "
                "it, so the fixture writes one file with the bit and one without"
            ),
            "ansible/tests/leakguard.py": (
                "the pytest plugin that installs the session-wide recording shims before the "
                "first test runs; it is the layer `lib.proc_testing`'s stubs are prepended over"
            ),
        },
    ),
    Census(
        name="tests-build-no-path-prefix-by-hand",
        reason=(
            "The `PATH` half of the proc_testing rule. A hit is a string putting something in "
            "front of an existing `PATH` value, in an `env=` dict or `monkeypatch.setenv`. "
            "Pass the directory as `run(..., stub_bin=...)`, or use `path_with(directory, "
            "env=...)`. A pinned value such as `/usr/bin:/bin` is the subject's environment."
        ),
        files=pytest_only_modules,
        offence=lambda s: handbuilt_path_prefixes(s.text),
        red=(
            Subject(
                "a.py", "env = {'PATH': f\"{d}{os.pathsep}{os.environ['PATH']}\"}\n"
            ),
            Subject("b.py", "env['PATH'] = str(d) + ':' + env['PATH']\n"),
            Subject(
                "c.py", "monkeypatch.setenv('PATH', f\"{d}:{os.environ['PATH']}\")\n"
            ),
        ),
        green=(
            Subject("a.py", "env = {'PATH': '/usr/bin:/bin'}\n"),
            Subject("b.py", "env = {'PATH': f'{d}:/usr/bin'}\n"),
            Subject("c.py", "return run(['bash', 's.sh'], stub_bin=stubs)\n"),
        ),
        min_matches=MODULE_FLOOR,
    ),
    Census(
        name="tests-launch-no-subprocess-without-a-deadline",
        reason=(
            "A child with no deadline that wedges parks the whole CI run instead of failing "
            "its own test. Call `lib.proc_testing.run`, which supplies `DEFAULT_TIMEOUT`, or "
            "pass `timeout=` with the reason at the line, `timeout=None` included where "
            "outliving every deadline is the point. That spelling is why this row has no "
            "`allow`: an exemption would cover a module's other launches too. "
            "`from subprocess import run` is a hit because it rebinds the bare `run` name. "
            "`Popen` is not covered; `_test_module_rules.py` records why at the marker after "
            "`_BOUNDABLE_LAUNCHES`."
        ),
        files=pytest_only_modules,
        offence=lambda s: unbounded_launches(s.text),
        red=(
            Subject("a.py", "subprocess.run(['x'], check=True)\n"),
            Subject("b.py", "subprocess.check_output(['x'])\n"),
            Subject("c.py", "sp.call(['x'])\n"),
            Subject("d.py", "import subprocess\nfrom subprocess import run\n"),
        ),
        green=(
            Subject("a.py", "subprocess.run(['x'], timeout=5)\n"),
            Subject("b.py", "subprocess.run(['x'], **kw)\n"),
            Subject("c.py", "subprocess.Popen(['x'])\n"),
            Subject("d.py", "run(['x'])\nclient.run(['x'])\n"),
            Subject("e.py", '"""subprocess.run([x]) used to be here."""\n'),
        ),
        min_matches=MODULE_FLOOR,
        must_find=frozenset(
            {
                ".claude/hooks/tests/test_hook_scripts_executable.py",
                "ansible/tests/_helpers.py",
                "ansible/tests/repo/test_testpaths_covers_every_test_file.py",
                "scripts/deploy_tools/tests/test_land_tools.py",
                "scripts/lib/tests/test_git.py",
            }
        ),
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
