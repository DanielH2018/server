"""Every catalogued entry point under `scripts/` answers `--help` with exit 0.

WHY THIS IS A SUBPROCESS TEST AND NOT A STATIC ONE. What it checks is the answer, not the
presence of an argparse call. Five scripts had a `--help` path and still failed: two hung
waiting on stdin or a network, one died resolving a kubeconfig at import, and `deploy.sh` ran
its staleness gate first and answered with exit 4 and a `git rebase` remedy. A static check for
`add_argument` passes on all five. Issue #2854 filed the class.

WHAT COUNTS AS AN ENTRY POINT. `lib.script_classify.classify` reads the tree for how each
script is reached, and everything it does not call a library is one -- except a Python module
with no `if __name__ == "__main__"` guard, which cannot be run at all. Thirteen of those are
misclassified `adhoc` because they sit in a package whose siblings import them relatively, so
the catalog cannot see the importer (filed as #3020); they are excluded here rather than given
a `--help` they have no way to print.

RUNTIME. Each script runs under this interpreter with `scripts/` on `PYTHONPATH`, not under
`uv run` -- 89 `uv run` calls cost a minute and a half where direct invocation costs ten
seconds. The environment is otherwise the caller's, which is the point: a `--help` that needs
a kubeconfig, a cluster or a lock is the failure this catches.

Run: uv run pytest scripts/lib/tests/test_entry_points_answer_help.py
"""

import os
import subprocess
import sys

import pytest

from lib.cli_help import HELP_FLAGS, answer_help, wants_help
from lib.repo_paths import REPO, SCRIPTS
from lib.script_classify import by_name, classify, file_text
from lib.script_classify import _has_main_guard as has_main_guard

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
# docstring. Two scripts hung indefinitely before #2854.
HELP_TIMEOUT_S = 60


def _entry_points():
    """`{name: path}` for every catalogued, runnable entry point under `scripts/`."""
    paths = by_name(SCRIPTS)
    found = {}
    for name, (kind, _why) in classify().items():
        if kind == "library":
            continue
        path = paths[name]
        if name.endswith(".py") and not has_main_guard(file_text(path)):
            continue
        found[name] = path
    return found


ENTRY_POINTS = _entry_points()


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
    env = {
        **os.environ,
        "PYTHONPATH": f"{SCRIPTS}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
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
