"""`cli_help.warn` and `cli_help.error` print the prefix argparse prints for a bad argument (#4447)."""

import argparse

import pytest

from lib.cli_help import error, warn

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
