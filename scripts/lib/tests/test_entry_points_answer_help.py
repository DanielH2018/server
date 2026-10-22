"""Every catalogued entry point under `scripts/` answers `--help` with exit 0.

WHY THIS IS A SUBPROCESS TEST AND NOT A STATIC ONE. What it checks is the answer, not the
presence of an argparse call. Five scripts had a `--help` path and still failed: two hung
waiting on stdin or a network, one died resolving a kubeconfig at import, and `deploy.sh` ran
its staleness gate first and answered with exit 4 and a `git rebase` remedy. A static check for
`add_argument` passes on all five.

WHAT COUNTS AS AN ANSWER. Exit 0 with output is not enough: a script that ignores `--help` and
runs its whole check exits 0 with output too, and `validate_ha_config.py --help` passed that way
while it printed `Home Assistant config OK` (#5579). The answer must carry the first line of the
entry point's docstring, which `lib.cli_help.answer_help` prints, or a `usage:` line, which
argparse prints. A shell script's docstring is its leading comment block.

WHAT COUNTS AS AN ENTRY POINT. `lib.script_classify.classify` reads the tree for how each
script is reached, and everything it does not call a library is one. A Python module with no
`if __name__ == "__main__"` guard cannot be run at all, and the classifier calls every one
of those a library -- through the importing package's own directory, through a relative import,
or by naming the tests that are its only importers. The reverse does not hold. The classifier
also calls a module a library once another script imports it, guard or not, and
`ha_state_model.py`, `await_ci.py`, `fanout.py` and `land_tags.py` are each imported and run
directly; `land_tags.py`'s run only points at `land_tags_cli.py` (#4945). So a library with its
own `__main__` guard counts as an entry point too (#4405).

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

import ast
import importlib
import os
import subprocess
import sys

import pytest

from lib.cli_help import HELP_FLAGS, answer_help, wants_help
from lib.repo_paths import REPO, SCRIPTS
from lib.script_classify import _has_main_guard, by_name, classify, file_text

# Non-vacuity: the census reads the tree, so a classifier change or a directory move can empty
# it, and `all(...)` over nothing passes. The first five are the shapes the census must keep --
# a shell shim, a Python gate, an adhoc tool, a scheduled generator, and the one whose
# `--help` answer was the reason for the test. The next four are imported by other scripts
# and also run directly, so the classifier calls them libraries; they are here so the
# `__main__`-guard half of `_entry_points` cannot lose them silently (#4405).
# `ha_state_model.py` broke three times on one import (#674). `land_tags_cli.py` is the CLI
# that moved out of `land_tags.py` (#4945).
MUST_FIND = frozenset(
    {
        "deploy.sh",
        "land.sh",
        "probe.py",
        "findings.py",
        "export_grafana_dashboards.py",
        "ha_state_model.py",
        "await_ci.py",
        "fanout.py",
        "land_tags.py",
        "land_tags_cli.py",
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
        # `_has_main_guard` is the classifier's own test for "this module can be run", so the
        # census and `classify` cannot disagree about what a guard is. `classify` also returns
        # a few test fixtures that `by_name` does not catalogue. They are libraries with no
        # path here, so the lookup checks membership first.
        or (name in paths and _has_main_guard(file_text(paths[name])))
    }


ENTRY_POINTS = _entry_points()


def _pythonpath_without_scripts():
    """The caller's `PYTHONPATH` minus `scripts/`, so only a module's bootstrap can supply it."""
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    return os.pathsep.join(
        e for e in entries if e and os.path.realpath(e) != os.path.realpath(SCRIPTS)
    )


def _doc_first_line(path):
    """The first line of an entry point's docstring, or "" when it has none.

    A Python module's docstring is its own. A shell script's is its leading comment block,
    which every shell entry point here prints for `--help`: the first comment line after the
    shebang that carries text.
    """
    text = path.read_text()
    if path.suffix == ".py":
        doc = (ast.get_docstring(ast.parse(text)) or "").strip()
        return doc.splitlines()[0].strip() if doc else ""
    for line in text.splitlines()[1:]:
        if not line.startswith("#"):
            return ""
        if line.lstrip("#").strip():
            return line.lstrip("#").strip()
    return ""


def _answers_help(stdout, path):
    """Whether `stdout` is a help answer rather than a job's output.

    exit 0 with any output is what a script that ignores `--help` and runs its whole check
    gives too (#5579), so the answer must carry the docstring's first line, which
    `answer_help` prints, or a `usage:` line, which argparse and the shell scripts print.
    """
    first = _doc_first_line(path)
    if first and first in stdout:
        return True
    return any(
        line.strip().lower().startswith("usage:") for line in stdout.splitlines()
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
    assert _answers_help(r.stdout, path), (
        f"{name} --help exited 0, but its stdout carries neither its docstring's first line "
        "nor a `usage:` line, so it ran its job instead of answering (#5579). Put "
        f"`lib.cli_help.answer_help` ahead of that work.\nstdout: {r.stdout[-400:]}"
    )


# The entry points the test above once waited on: each ignored `--help` and ran its whole check,
# which still exits 0 with output, so the subprocess test passed while
# `k8s_manifests.py --help` took 7s to print its summary line (#5564).
WORK_BEFORE_HELP = (
    "validate.k8s_manifests",
    "validate.setup_templates",
    "validate.shell_templates",
    "validate.unit_templates",
    "validate.compose_templates",
    "validate.grafana_dashboards",
    "validate.jinja_bash_collisions",
    "home_assistant.validate_ha_config",
    "secrets_mgmt.secret_bearing_host_paths",
    "dev.k8s_autodeploy_counts",
)


@pytest.mark.parametrize("name", WORK_BEFORE_HELP)
def test_an_entry_point_answers_help_before_its_work(name, capsys):
    module = importlib.import_module(name)
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == (module.__doc__ or "").strip() + "\n"


# -- cc-wait's repo probes: the one exit that is not 0 ------------------------------------

WAIT_SOURCES = REPO / ".claude" / "wait-sources"
PROBES = sorted(path.name for path in WAIT_SOURCES.iterdir() if path.is_file())
MUST_FIND_PROBES = frozenset({"land", "deploy", "fanout"})

# cc-wait reports a refused `--describe` with only the last 400 characters of its stderr
# (dotfiles `cc_wait/repo.py`, `ProbeWait.describe`), so the usage has to land inside them.
CC_WAIT_STDERR_TAIL = 400


def _probe(name: str, *argv: str) -> subprocess.CompletedProcess:
    """Run a probe the way cc-wait does: its tracked shim, from the repo root."""
    env = {**os.environ, "PYTHONPATH": _pythonpath_without_scripts()}
    return subprocess.run(
        [str(WAIT_SOURCES / name), *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=HELP_TIMEOUT_S,
        env=env,
    )


def test_the_census_still_finds_every_cc_wait_probe():
    missing = MUST_FIND_PROBES - set(PROBES)
    assert not missing, f"the probe census lost {sorted(missing)}"


@pytest.mark.parametrize("name", PROBES, ids=str)
def test_a_probe_refuses_help_under_describe_with_its_usage_on_stderr(name):
    """`cc-wait land --help` runs `land --describe --help` and parses its stdout as JSON (#4376)."""
    r = _probe(name, "--describe", "--help")
    assert (r.returncode, r.stdout) == (64, ""), (
        f"{name} --describe --help exited {r.returncode}; cc-wait reads exit 0 as a "
        f"description.\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    )
    assert "usage:" in r.stderr.strip()[-CC_WAIT_STDERR_TAIL:], r.stderr


@pytest.mark.parametrize("name", PROBES, ids=str)
def test_a_probe_still_answers_help_without_describe(name):
    r = _probe(name, "--help")
    assert r.returncode == 0, r.stderr[-400:]
    assert r.stdout.startswith("usage:"), r.stdout[:400]


# -- the helper's own rules ----------------------------------------------------------------


@pytest.mark.parametrize("flag", HELP_FLAGS)
def test_either_help_flag_is_recognised(flag):
    assert wants_help(["--pr", "7", flag])


def test_a_flag_that_merely_contains_help_is_not_a_help_request():
    """The reject half. `--helpful` and `--no-help` are somebody else's flags."""
    assert not wants_help(["--helpful", "--no-help", "help"])


def test_a_shell_entry_point_answers_with_its_header_comment(tmp_path):
    """The shell half of `_answers_help`: the header comment counts, a job's output does not."""
    script = tmp_path / "fake.sh"
    script.write_text(
        "#!/usr/bin/env bash\n#\n# Check the fakes.\n#\n# More.\nexit 0\n"
    )
    assert _doc_first_line(script) == "Check the fakes."
    assert _answers_help("Check the fakes.\n\nMore.\n", script)
    assert not _answers_help("4 template(s) checked, 0 failure(s).\n", script)


def test_answer_help_prints_the_doc_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        answer_help("  What it does.\n", ["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "What it does.\n"


def test_answer_help_returns_without_printing_when_no_flag_asks(capsys):
    answer_help("What it does.", ["--pr", "7"])
    assert capsys.readouterr().out == ""
