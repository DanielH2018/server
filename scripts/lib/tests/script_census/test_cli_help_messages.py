"""`cli_help.warn` and `cli_help.error` print the prefix argparse prints for a bad argument (#4447)."""

import argparse

import pytest

from lib.cli_help import ArgumentParser, error, warn
from lib.exit_codes import USAGE_ERROR

SCRIPT = "/repo/scripts/secrets_mgmt/secret_rotation.py"


def test_error_prefix_matches_argparse_byte_for_byte(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [SCRIPT])
    with pytest.raises(SystemExit):
        argparse.ArgumentParser().error("boom")
    argparse_line = capsys.readouterr().err.splitlines()[-1]

    error("boom")

    assert capsys.readouterr().err == argparse_line + "\n"


def test_warn_names_the_running_script(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [SCRIPT, "audit"])
    warn("push lost")
    assert capsys.readouterr().err == "secret_rotation.py: warning: push lost\n"


def test_error_returns_rather_than_exiting(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [SCRIPT])
    assert error("boom") is None
    assert capsys.readouterr().out == ""


def test_prog_names_the_wrapper_the_operator_typed(monkeypatch, capsys):
    """`land.sh` execs `land.py`; the line names the first (#4478)."""
    monkeypatch.setattr("sys.argv", ["/repo/scripts/deploy_tools/land.py"])
    error("lock busy", prog="land.sh")
    warn("tree is behind", prog="land.sh")
    assert capsys.readouterr().err == (
        "land.sh: error: lock busy\nland.sh: warning: tree is behind\n"
    )


def test_a_usage_error_prints_the_parsers_example_after_the_message(capsys):
    parser = ArgumentParser(prog="tool", example="tool run --target box")
    parser.add_argument("--target", required=True)
    with pytest.raises(SystemExit) as exit_:
        parser.parse_args([])
    assert exit_.value.code == USAGE_ERROR
    assert capsys.readouterr().err.endswith(
        "tool: error: the following arguments are required: --target\n"
        "example: tool run --target box\n"
    )


def test_a_parser_without_an_example_prints_none(capsys):
    parser = ArgumentParser(prog="tool")
    parser.add_argument("--target", required=True)
    with pytest.raises(SystemExit):
        parser.parse_args([])
    assert "example:" not in capsys.readouterr().err
