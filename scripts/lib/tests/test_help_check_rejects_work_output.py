"""The `--help` subprocess test refuses an entry point that runs its job instead of answering.

`test_the_entry_point_answers_help_with_exit_zero` accepted any run that exited 0 and printed
something, so `validate_ha_config.py --help` passed while printing `Home Assistant config OK`
after a full validation (#5579). These tests hand it a fake entry point, registered in its own
census, and assert on its verdict: a help answer passes and a job's output fails.

THE SEAM. The fake is registered as `ENTRY_POINTS[name] = path`, and its docstring lives in that
file, so a check that reads the docstring from the entry point's own file sees it.

Run: uv run pytest scripts/lib/tests/test_help_check_rejects_work_output.py
"""

import textwrap

import pytest
import test_entry_points_answer_help as help_census

DOC_FIRST_LINE = "Check the compose templates against the shared macros."

# What `compose_templates.py --help` printed while it ran its whole validation (#5579). It
# shares no word with the docstring's first line and carries no `usage:`.
WORK_OUTPUT = "4 template(s) checked, 0 failure(s)."


def _fake(tmp_path, monkeypatch, name, body):
    """Register a fake entry point `name` whose module docstring opens with DOC_FIRST_LINE."""
    path = tmp_path / name
    source = f'"""{DOC_FIRST_LINE}\n\nLonger explanation.\n"""\n\n' + textwrap.dedent(
        body
    )
    path.write_text(source)
    monkeypatch.setitem(help_census.ENTRY_POINTS, name, path)
    return name


def _verdict(name):
    """Run the census's `--help` check on `name`, returning None on a pass or the failure text."""
    try:
        help_census.test_the_entry_point_answers_help_with_exit_zero(name)
    except (AssertionError, pytest.fail.Exception) as exc:
        return str(exc)
    return None


def test_the_help_check_refuses_an_entry_point_that_runs_its_job(tmp_path, monkeypatch):
    # Controls: the two shapes a real help answer takes must keep passing, so a check that
    # rejects everything does not satisfy this test.
    docstring_answer = _fake(
        tmp_path,
        monkeypatch,
        "fake_doc_answer.py",
        """
        import sys
        if "--help" in sys.argv[1:]:
            print(__doc__.strip())
            raise SystemExit(0)
        print("ran the job")
        """,
    )
    argparse_answer = _fake(
        tmp_path,
        monkeypatch,
        "fake_argparse_answer.py",
        """
        import argparse
        argparse.ArgumentParser().parse_args()
        """,
    )
    assert _verdict(docstring_answer) is None
    assert _verdict(argparse_answer) is None

    job = _fake(
        tmp_path,
        monkeypatch,
        "fake_runs_its_job.py",
        f"""
        print({WORK_OUTPUT!r})
        """,
    )
    assert _verdict(job) is not None, (
        "the --help check passed an entry point that ignored --help, ran its job and printed "
        f"{WORK_OUTPUT!r}; it must require the docstring's first line or a usage: line"
    )
