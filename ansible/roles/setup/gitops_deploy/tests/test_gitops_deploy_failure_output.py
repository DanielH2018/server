"""What a failed subprocess leaves behind: run()'s error string and the alerts that embed it.

ansible-playbook prints the failing TASK, the `fatal:` line and the PLAY RECAP to stdout, so
these tests drive real `sh` processes rather than fakes — a stub that returns a
CompletedProcess would prove the formatting and not the capture.
"""

import pathlib

import pytest

FATAL = 'fatal: [daniel-box]: FAILED! => {"msg": "the task that broke"}'


def _fail(gitops_deploy, tmp_path: pathlib.Path, script: str) -> str:
    """Run ``script`` under sh, expect it to fail, and return the RuntimeError's text.

    The script goes in a FILE, never `sh -c <script>`: the error string opens with the argv,
    so an inline script would put its own output there and every assertion below would pass
    without the process output being captured at all.
    """
    path = tmp_path / "fail.sh"
    path.write_text(script)
    with pytest.raises(RuntimeError) as excinfo:
        gitops_deploy.run(["sh", str(path)], cwd=str(tmp_path))
    return str(excinfo.value)


def test_a_failure_reported_only_on_stdout_names_the_failing_task(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    message = _fail(gitops_deploy, tmp_path, f"echo '{FATAL}'; exit 2")
    assert FATAL in message
    assert "-> 2" in message


def test_the_error_keeps_the_END_of_an_overlong_stdout(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """A head slice passes every short-output test and carries nothing on a real 20-minute run.

    The padding is longer than the bound, so only a tail-keeping slice reaches the `fatal:`
    line — which ansible prints near the end, after every ok/skipped task.
    """
    padding = "TASK [something ok] " + "x" * gitops_deploy.RUN_ERROR_STDOUT_TAIL
    message = _fail(
        gitops_deploy, tmp_path, f"echo '{padding}'; echo '{FATAL}'; exit 2"
    )
    assert FATAL in message
    assert "truncated" in message
    assert len(message) < 2 * gitops_deploy.RUN_ERROR_STDOUT_TAIL


def test_stderr_is_still_reported(gitops_deploy, tmp_path: pathlib.Path) -> None:
    """The control for the two above: adding stdout must not drop what was already carried."""
    message = _fail(gitops_deploy, tmp_path, "echo 'ssh: connect refused' >&2; exit 3")
    assert "ssh: connect refused" in message
    assert "-> 3" in message


def test_a_succeeding_command_still_returns_its_stdout(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    assert gitops_deploy.run(["sh", "-c", "echo hi"], cwd=str(tmp_path)) == "hi"


def test_tail_passes_short_text_through(gitops_deploy) -> None:
    assert gitops_deploy._tail("one\ntwo", 100) == "one\ntwo"


def test_tail_drops_the_head_of_long_text(gitops_deploy) -> None:
    tailed = gitops_deploy._tail("head-line\n" + "y" * 50 + "\nlast-line", 60)
    assert "head-line" not in tailed
    assert "last-line" in tailed


def test_the_alert_excerpt_keeps_the_argv_line_and_the_tail(gitops_deploy) -> None:
    exc = RuntimeError(
        "uv run ansible-playbook ansible/deploy.yml -> 2\n" + "z" * 5000 + f"\n{FATAL}"
    )
    excerpt = gitops_deploy._alert_excerpt(exc)
    assert excerpt.startswith("uv run ansible-playbook ansible/deploy.yml -> 2")
    assert FATAL in excerpt


def test_the_broad_alert_fits_discords_head_slice_with_the_action_line_intact(
    gitops_deploy,
) -> None:
    """discord_post cuts at message[:1900], keeping the head — so an unbounded error string
    would evict the remediation prose rather than truncate itself."""
    exc = RuntimeError(
        "uv run ansible-playbook ansible/deploy.yml -> 2\n" + "z" * 50000
    )
    message = gitops_deploy.broad_failure_alert(
        "daniel-box", "ansible/deploy.yml", [], "2d25ced3" * 5, exc
    )
    assert len(message) <= 1900
    assert "fix forward and re-run that playbook by hand" in message
    assert "nothing was rolled back" in message


def test_the_broad_alert_carries_the_failure_detail(gitops_deploy) -> None:
    exc = RuntimeError(f"uv run ansible-playbook ansible/deploy.yml -> 2\n{FATAL}")
    message = gitops_deploy.broad_failure_alert(
        "daniel-box", "ansible/initial_setup.yml", ["k3s"], "2d25ced3" * 5, exc
    )
    assert FATAL in message
    assert "--tags `k3s`" in message
