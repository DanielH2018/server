"""Every complaint `deploy.sh` prints names `deploy.sh` and a severity, whatever argv[0] is (#4478)."""

from deploy_tools.deploy_lib.messages import say_error, say_warning


def test_say_error_names_deploy_sh_and_keeps_its_continuation_lines(
    monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", ["/repo/scripts/deploy_tools/deploy_cli.py"])
    say_error(
        "could not take /var/lock/x -- nothing was deployed.", "  Check the lock."
    )
    assert capsys.readouterr().err == (
        "deploy.sh: error: could not take /var/lock/x -- nothing was deployed.\n"
        "  Check the lock.\n"
    )


def test_say_warning_names_deploy_sh(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["/repo/scripts/deploy_tools/deploy_cli.py"])
    say_warning("did not check the k8s_unapplied ledger")
    assert capsys.readouterr().err == (
        "deploy.sh: warning: did not check the k8s_unapplied ledger\n"
    )
