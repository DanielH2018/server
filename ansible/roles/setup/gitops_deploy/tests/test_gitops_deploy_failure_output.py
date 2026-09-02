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


TASK_HEADER = "TASK [k8s/rollout-drain : Wait for the queued rollouts to finish] ****"
RECAP = (
    "PLAY RECAP ****\n"
    "daniel-box : ok=1950 changed=254 unreachable=0 failed=1 skipped=1032 rescued=0 ignored=0"
)


def _timing_table(chars: int) -> str:
    """What profile_tasks prints after the recap, padded past ``chars``."""
    rows = ["TASKS RECAP ****"]
    while len("\n".join(rows)) < chars:
        rows.append(
            f"k8s/manifests : Render manifests for service-{len(rows)} ------- 9.40s"
        )
    return "\n".join(rows)


def test_the_error_keeps_the_fatal_line_behind_an_overlong_stdout(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """A head slice passes every short-output test and carries nothing on a real 20-minute run.

    Thousands of ok tasks precede the one that fails, so a slice that keeps the start of the
    output never reaches the `fatal:` line.
    """
    ok_tasks = "\n".join(
        f"TASK [ok task {n}] ****\nok: [daniel-box]"
        for n in range(gitops_deploy.RUN_ERROR_STDOUT_CHARS // 30)
    )
    (tmp_path / "out.txt").write_text(f"{ok_tasks}\n{TASK_HEADER}\n{FATAL}\n{RECAP}\n")
    message = _fail(gitops_deploy, tmp_path, "cat out.txt; exit 2")
    assert TASK_HEADER in message
    assert FATAL in message
    assert "TASK [ok task 0]" not in message, "the ok tasks before it are the padding"
    assert len(message) < 2 * gitops_deploy.RUN_ERROR_STDOUT_CHARS


def test_the_error_keeps_the_fatal_line_ahead_of_a_profile_tasks_timing_table(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """Issue #907: the timing table after the recap is bigger than the budget on deploy.yml.

    A positional tail of that output is `ok=1950 failed=1` plus twenty timing rows, and the
    task that failed is the one thing not in it.
    """
    table = _timing_table(gitops_deploy.RUN_ERROR_STDOUT_CHARS)
    (tmp_path / "out.txt").write_text(f"{TASK_HEADER}\n{FATAL}\n{RECAP}\n{table}\n")
    message = _fail(gitops_deploy, tmp_path, "cat out.txt; exit 2")
    assert TASK_HEADER in message
    assert FATAL in message
    assert "failed=1" in message, "the recap is still worth carrying"
    assert "TASKS RECAP" not in message, "the timing table is not"
    assert len(message) < 2 * gitops_deploy.RUN_ERROR_STDOUT_CHARS


def test_stdout_with_no_fatal_line_is_a_plain_tail(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """The control for the extractor: output it cannot parse keeps the end, as before."""
    lines = "\n".join(f"line {n}" for n in range(2000))
    (tmp_path / "out.txt").write_text(lines + "\n")
    message = _fail(gitops_deploy, tmp_path, "cat out.txt; exit 2")
    assert "line 1999" in message
    assert "line 0\n" not in message


def test_the_failing_task_is_the_last_one_not_ignored(gitops_deploy) -> None:
    """An `ignore_errors` failure prints `fatal:` too; `...ignoring` is what tells them apart."""
    text = (
        "TASK [probe : Try the optional thing] ****\n"
        'fatal: [daniel-box]: FAILED! => {"msg": "optional, ignored"}\n'
        "...ignoring\n"
        f"{TASK_HEADER}\n{FATAL}\n{RECAP}"
    )
    task, rest = gitops_deploy._failing_task(text)
    assert task == f"{TASK_HEADER}\n{FATAL}"
    assert rest.startswith("PLAY RECAP")
    ignored_only = "\n".join(text.splitlines()[:3]) + f"\n{RECAP}"
    assert gitops_deploy._failing_task(ignored_only) is None


def test_the_failing_task_drops_the_ok_items_of_a_loop_and_keeps_its_failed_ones(
    gitops_deploy,
) -> None:
    text = (
        "TASK [k8s/manifests : Apply each manifest] ****\n"
        "ok: [daniel-box] => (item=a)\n"
        "ok: [daniel-box] => (item=b)\n"
        'failed: [daniel-box] (item=c) => {"msg": "c broke"}\n'
        "ok: [daniel-box] => (item=d)\n"
        'fatal: [daniel-box]: FAILED! => {"msg": "One or more items failed"}\n'
        f"{RECAP}"
    )
    task, _ = gitops_deploy._failing_task(text)
    assert task.startswith("TASK [k8s/manifests : Apply each manifest]")
    assert "(item=c)" in task
    assert "One or more items failed" in task
    assert "(item=a)" not in task


def test_an_unreachable_host_is_a_failing_task_too(gitops_deploy) -> None:
    text = (
        "TASK [Gathering Facts] ****\n"
        'fatal: [daniel-pi]: UNREACHABLE! => {"msg": "ssh: connect refused"}\n'
        "NO MORE HOSTS LEFT ****\n"
        f"{RECAP}"
    )
    task, _ = gitops_deploy._failing_task(text)
    assert task == (
        "TASK [Gathering Facts] ****\n"
        'fatal: [daniel-pi]: UNREACHABLE! => {"msg": "ssh: connect refused"}'
    )


def test_an_overlong_failing_task_is_head_cut_so_its_name_survives(
    gitops_deploy,
) -> None:
    text = (
        f"{TASK_HEADER}\n"
        + 'fatal: [daniel-box]: FAILED! => {"msg": "'
        + "m" * 9000
        + '"}'
    )
    detail = gitops_deploy._failure_detail(text, 4000)
    assert detail.startswith(TASK_HEADER)
    assert detail.endswith(gitops_deploy.TRUNCATED)
    assert len(detail) <= 4000 + len(gitops_deploy.TRUNCATED) + 1


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


def test_head_passes_short_text_through_and_cuts_long_text_at_a_line(
    gitops_deploy,
) -> None:
    assert gitops_deploy._head("one\ntwo", 100) == "one\ntwo"
    cut = gitops_deploy._head("first-line\n" + "y" * 50 + "\nlast-line", 60)
    assert cut.startswith("first-line\n")
    assert "last-line" not in cut
    assert cut.endswith(gitops_deploy.TRUNCATED)


def test_the_alert_excerpt_keeps_the_failing_task_ahead_of_a_long_stderr(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """The Discord post is 700 chars of a 4000+4000 error string.

    A tail of that is stderr's deprecation warnings, which is what the 21:26 alert on
    2026-09-02 carried (issue #907). The failing task is the part worth the budget.
    """
    stderr = "\n".join(
        "[DEPRECATION WARNING]: Conditionals should not be surrounded by templating delimiters"
        for _ in range(60)
    )
    (tmp_path / "out.txt").write_text(
        f"{TASK_HEADER}\n{FATAL}\n{RECAP}\n{_timing_table(4000)}\n"
    )
    (tmp_path / "err.txt").write_text(stderr + "\n")
    path = tmp_path / "fail.sh"
    path.write_text("cat out.txt; cat err.txt >&2; exit 2")
    with pytest.raises(RuntimeError) as excinfo:
        gitops_deploy.run(["sh", str(path)], cwd=str(tmp_path))
    excerpt = gitops_deploy._alert_excerpt(excinfo.value)
    assert excerpt.startswith("sh ")
    assert TASK_HEADER in excerpt
    assert FATAL in excerpt
    assert (
        len(excerpt)
        <= gitops_deploy.ALERT_EXCERPT_CHARS + len(gitops_deploy.TRUNCATED) + 1
    )
