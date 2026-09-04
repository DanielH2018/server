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


def test_land_primary_names_the_checkout(monkeypatch, tmp_path):
    """`LAND_PRIMARY` is how a test aims a landing away from the live checkout."""
    monkeypatch.setenv("LAND_PRIMARY", str(tmp_path))
    assert options.parse_args(["--pr", "7"], "d").primary == tmp_path


def test_primary_defaults_to_the_primary_checkout(monkeypatch):
    monkeypatch.delenv("LAND_PRIMARY", raising=False)
    assert options.parse_args(["--pr", "7"], "d").primary == options.PRIMARY_CHECKOUT


def test_land_merge_poll_shortens_the_merge_wait(monkeypatch):
    monkeypatch.setenv("LAND_MERGE_POLL", "2")
    assert options.parse_args(["--pr", "7"], "d").merge_poll == 2


def test_merge_poll_defaults_to_30s(monkeypatch):
    monkeypatch.delenv("LAND_MERGE_POLL", raising=False)
    assert options.parse_args(["--pr", "7"], "d").merge_poll == 30


def test_help_prints_the_description(capsys):
    with pytest.raises(SystemExit) as exc:
        options.parse_args(["--help"], "Verdicts printed on stdout: settled")
    assert exc.value.code == 0
    assert "Verdicts printed on stdout" in capsys.readouterr().out
