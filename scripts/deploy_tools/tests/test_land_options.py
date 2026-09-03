"""Argument parsing, and the budgets a test can shorten without an env knob.

Run: uv run pytest scripts/deploy_tools/tests/test_land_options.py
"""

from __future__ import annotations

import pytest

from deploy_tools.land_lib import options


def test_pr_is_required():
    with pytest.raises(SystemExit) as exc:
        options.parse_args(["--arm-merge"], "desc")
    assert exc.value.code == 2


def test_flags_land_on_the_options():
    o = options.parse_args(
        [
            "--pr",
            "7",
            "--since",
            "abc",
            "--arm-merge",
            "--await-merge",
            "--subject",
            "S",
        ],
        "d",
    )
    assert (o.pr, o.since, o.arm_merge, o.await_merge, o.subject) == (
        "7",
        "abc",
        True,
        True,
        "S",
    )


def test_help_prints_the_description(capsys):
    with pytest.raises(SystemExit) as exc:
        options.parse_args(["--help"], "Verdicts printed on stdout: settled")
    assert exc.value.code == 0
    assert "Verdicts printed on stdout" in capsys.readouterr().out
