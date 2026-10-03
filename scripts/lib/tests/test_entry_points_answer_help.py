"""Every catalogued entry point under `scripts/` answers `--help` with exit 0.

WHY THIS IS A SUBPROCESS TEST AND NOT A STATIC ONE. What it checks is the answer, not the
presence of an argparse call. Five scripts had a `--help` path and still failed: two hung
waiting on stdin or a network, one died resolving a kubeconfig at import, and `deploy.sh` ran
its staleness gate first and answered with exit 4 and a `git rebase` remedy. A static check for
`add_argument` passes on all five.

WHAT COUNTS AS AN ENTRY POINT. `lib.script_classify.classify` reads the tree for how each
script is reached, and everything it does not call a library is one. A Python module with no
`if __name__ == "__main__"` guard cannot be run at all, and the classifier calls every one
of those a library -- through the importing package's own directory, through a relative import,
or by naming the tests that are its only importers. So this test filters on the verdict
alone.

RUNTIME. Each script runs under this interpreter, not under `uv run` -- one `uv run` per entry
point cost a minute and a half where direct invocation costs ten seconds, over a census this
size. The environment is otherwise the caller's, which is the point: a `--help` that needs a
kubeconfig, a cluster or a lock is the failure this catches.

NO `scripts/` ON `PYTHONPATH`. Each script runs from the repo root with `scripts/` removed from
`PYTHONPATH`, the way a cron or `uv run python scripts/...` runs it. An `import lib...` then
resolves only through the module's own `sys.path` bootstrap, so deleting that bootstrap fails
this test for that entry point. With `scripts/` on the path every such import succeeded
regardless, and the test could not see the one failure a cron hits first (#3296).

Run: uv run pytest scripts/lib/tests/test_entry_points_answer_help.py
"""

import os
import subprocess
import sys

import pytest

from lib.cli_help import HELP_FLAGS, answer_help, wants_help
from lib.repo_paths import REPO, SCRIPTS
from lib.script_classify import by_name, classify

# Non-vacuity: the census reads the tree, so a classifier change or a directory move can empty
# it, and `all(...)` over nothing passes. These five are the shapes the census must keep --
# a shell shim, a Python gate, an adhoc tool, a scheduled generator, and the one whose
# `--help` answer was the reason for the test.
MUST_FIND = frozenset(
    {
        "deploy.sh",
        "land.sh",
        "probe.py",
        "findings.py",
        "export_grafana_dashboards.py",
    }
)

# A `--help` that has to reach the cluster, the network or a lock is the defect, so the
# per-script budget is generous only by the standard of a script that answers from its own
# docstring.
HELP_TIMEOUT_S = 60


def _entry_points():
    """`{name: path}` for every catalogued, runnable entry point under `scripts/`."""
    paths = by_name(SCRIPTS)
    return {
        name: paths[name]
        for name, (kind, _why) in classify().items()
        if kind != "library"
    }


ENTRY_POINTS = _entry_points()


def _pythonpath_without_scripts():
    """The caller's `PYTHONPATH` minus `scripts/`, so only a module's bootstrap can supply it."""
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    return os.pathsep.join(
        e for e in entries if e and os.path.realpath(e) != os.path.realpath(SCRIPTS)
    )


def test_the_census_still_finds_the_entry_points_it_is_measured_against():
    missing = MUST_FIND - set(ENTRY_POINTS)
    assert not missing, f"the entry-point census lost {sorted(missing)}"
    assert len(ENTRY_POINTS) >= 70, len(ENTRY_POINTS)


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS), ids=str)
def test_the_entry_point_answers_help_with_exit_zero(name):
    path = ENTRY_POINTS[name]
    argv = (
        [sys.executable, str(path), "--help"]
        if name.endswith(".py")
        else ["bash", str(path), "--help"]
    )
    env = {**os.environ, "PYTHONPATH": _pythonpath_without_scripts()}
    try:
        r = subprocess.run(
            argv,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{name} --help did not answer within {HELP_TIMEOUT_S}s: it is doing work before "
            "it looks at argv. Put `lib.cli_help.answer_help` ahead of that work."
        )
    assert r.returncode == 0, (
        f"{name} --help exited {r.returncode}; a script must say what it is for from any "
        f"environment.\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    )
    assert r.stdout.strip(), f"{name} --help exited 0 and printed nothing to stdout"


# -- the helper's own rules ----------------------------------------------------------------


@pytest.mark.parametrize("flag", HELP_FLAGS)
def test_either_help_flag_is_recognised(flag):
    assert wants_help(["--pr", "7", flag])


def test_a_flag_that_merely_contains_help_is_not_a_help_request():
    """The reject half. `--helpful` and `--no-help` are somebody else's flags."""
    assert not wants_help(["--helpful", "--no-help", "help"])


def test_answer_help_prints_the_doc_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        answer_help("  What it does.\n", ["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "What it does.\n"


def test_answer_help_returns_without_printing_when_no_flag_asks(capsys):
    answer_help("What it does.", ["--pr", "7"])
    assert capsys.readouterr().out == ""
