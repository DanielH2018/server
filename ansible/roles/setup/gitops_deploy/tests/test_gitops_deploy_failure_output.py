"""What a failed subprocess leaves behind: run()'s error string and the alerts that embed it.

ansible-playbook prints the failing TASK, the `fatal:` line and the PLAY RECAP to stdout, so
these tests drive real `sh` processes rather than fakes — a stub that returns a
CompletedProcess would prove the formatting and not the capture.
"""

import pathlib
import subprocess

import pytest

import deploy_alert_text
import deploy_io
from gitops_markers import HOLD_CLEAR_CMD

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
        deploy_io.run(["sh", str(path)], cwd=str(tmp_path))
    return str(excinfo.value)


def test_a_failure_reported_only_on_stdout_names_the_failing_task(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    message = _fail(gitops_deploy, tmp_path, f"echo '{FATAL}'; exit 2")
    assert FATAL in message
    assert "-> 2" in message


TASK_HEADER = "TASK [k8s/manifests : Wait for the queued rollouts to finish] ****"
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
        for n in range(deploy_io.RUN_ERROR_STDOUT_CHARS // 30)
    )
    (tmp_path / "out.txt").write_text(f"{ok_tasks}\n{TASK_HEADER}\n{FATAL}\n{RECAP}\n")
    message = _fail(gitops_deploy, tmp_path, "cat out.txt; exit 2")
    assert TASK_HEADER in message
    assert FATAL in message
    assert "TASK [ok task 0]" not in message, "the ok tasks before it are the padding"
    assert len(message) < 2 * deploy_io.RUN_ERROR_STDOUT_CHARS


def test_the_error_keeps_the_fatal_line_ahead_of_a_profile_tasks_timing_table(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """The timing table after the recap is bigger than the budget on deploy.yml.

    A positional tail of that output is `ok=1950 failed=1` plus twenty timing rows, and the
    task that failed is the one thing not in it.
    """
    table = _timing_table(deploy_io.RUN_ERROR_STDOUT_CHARS)
    (tmp_path / "out.txt").write_text(f"{TASK_HEADER}\n{FATAL}\n{RECAP}\n{table}\n")
    message = _fail(gitops_deploy, tmp_path, "cat out.txt; exit 2")
    assert TASK_HEADER in message
    assert FATAL in message
    assert "failed=1" in message, "the recap is still worth carrying"
    assert "TASKS RECAP" not in message, "the timing table is not"
    assert len(message) < 2 * deploy_io.RUN_ERROR_STDOUT_CHARS


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
    found = deploy_io.failing_task(text)
    assert found is not None
    task, rest = found
    assert task == f"{TASK_HEADER}\n{FATAL}"
    assert rest.startswith("PLAY RECAP")
    ignored_only = "\n".join(text.splitlines()[:3]) + f"\n{RECAP}"
    assert deploy_io.failing_task(ignored_only) is None


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
    found = deploy_io.failing_task(text)
    assert found is not None
    task, _ = found
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
    found = deploy_io.failing_task(text)
    assert found is not None
    task, _ = found
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
    detail = deploy_io.failure_detail(text, 4000)
    assert detail.startswith(TASK_HEADER)
    assert detail.endswith(deploy_io.TRUNCATED)
    assert len(detail) <= 4000 + len(deploy_io.TRUNCATED) + 1


def test_stderr_is_still_reported(gitops_deploy, tmp_path: pathlib.Path) -> None:
    """The control for the two above: adding stdout must not drop what was already carried."""
    message = _fail(gitops_deploy, tmp_path, "echo 'ssh: connect refused' >&2; exit 3")
    assert "ssh: connect refused" in message
    assert "-> 3" in message


def test_a_succeeding_command_still_returns_its_stdout(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    assert deploy_io.run(["sh", "-c", "echo hi"], cwd=str(tmp_path)) == "hi"


def test_tail_passes_short_text_through(gitops_deploy) -> None:
    assert deploy_io.tail("one\ntwo", 100) == "one\ntwo"


def test_tail_drops_the_head_of_long_text(gitops_deploy) -> None:
    tailed = deploy_io.tail("head-line\n" + "y" * 50 + "\nlast-line", 60)
    assert "head-line" not in tailed
    assert "last-line" in tailed


def test_the_alert_excerpt_keeps_the_argv_line_and_the_tail(gitops_deploy) -> None:
    exc = RuntimeError(
        "uv run ansible-playbook ansible/deploy.yml -> 2\n" + "z" * 5000 + f"\n{FATAL}"
    )
    excerpt = deploy_alert_text.alert_excerpt(exc)
    assert excerpt.startswith("uv run ansible-playbook ansible/deploy.yml -> 2")
    assert FATAL in excerpt


# Nine bumps: the longest list a post has named.
NINE_BUMPS = {
    "bentopdf",
    "flaresolverr",
    "homepage",
    "freshrss",
    "home-assistant",
    "speedtest",
    "radarr",
    "prowlarr",
    "bazarr",
}


def test_the_broad_alert_fits_discords_head_slice_with_the_action_line_intact(
    gitops_deploy,
) -> None:
    """discord_post cuts at message[:1900], keeping the head — so an unbounded error string
    would evict the remediation prose rather than truncate itself. Measured with the widest
    dropped-bump list the post has had to name, since that line grows it."""
    exc = RuntimeError(
        "uv run ansible-playbook ansible/deploy.yml -> 2\n" + "z" * 50000
    )
    message = deploy_alert_text.broad_failure_alert(
        "daniel-box",
        "ansible/deploy.yml",
        [],
        "2d25ced3" * 5,
        exc,
        NINE_BUMPS,
    )
    assert len(message) <= 1900
    assert "fix forward and re-run that playbook by hand" in message
    assert "nothing was rolled back" in message
    assert f'--tags "{",".join(sorted(NINE_BUMPS))}"' in message


def test_the_broad_alert_carries_the_failure_detail(gitops_deploy) -> None:
    exc = RuntimeError(f"uv run ansible-playbook ansible/deploy.yml -> 2\n{FATAL}")
    message = deploy_alert_text.broad_failure_alert(
        "daniel-box",
        "ansible/initial_setup.yml",
        ["k3s"],
        "2d25ced3" * 5,
        exc,
        set(),
    )
    assert FATAL in message
    assert "--tags `k3s`" in message
    assert "not deployed" not in message, "a range with no bump names none"
    assert message.endswith(deploy_alert_text.hold_clear_note("2d25ced3" * 5)), (
        "the held planes are ledger lines, so the post names the Clear that drops them "
        "with the hold rather than an rm that leaves them behind"
    )
    assert f"`{HOLD_CLEAR_CMD} {'2d25ced3' * 5}`" in message, (
        "the shell clear takes the full held SHA and refuses any other"
    )
    assert "`rm /" not in message


def test_the_broad_k8s_alert_fits_the_head_slice_and_names_the_services_once(
    gitops_deploy,
    state,
) -> None:
    """The same 1900-char budget as its siblings, at the widest service list seen.

    The list is named once, in the command the operator runs: a second copy in the header
    spent the budget the error excerpt and the action line share.
    """
    exc = RuntimeError(
        "uv run ansible-playbook ansible/deploy.yml -> 2\n" + "z" * 50000
    )
    message = deploy_alert_text.broad_k8s_failure_alert(
        "daniel-box",
        "2d25ced3" * 5,
        NINE_BUMPS,
        exc,
        state.path("hold"),
    )
    assert len(message) <= 1900
    assert "Nothing was rolled back" in message
    assert message.endswith(deploy_alert_text.hold_clear_note("2d25ced3" * 5))
    assert message.count(",".join(sorted(NINE_BUMPS))) == 1
    assert message.count("bentopdf") == 1


def test_head_passes_short_text_through_and_cuts_long_text_at_a_line(
    gitops_deploy,
) -> None:
    assert deploy_io.head("one\ntwo", 100) == "one\ntwo"
    cut = deploy_io.head("first-line\n" + "y" * 50 + "\nlast-line", 60)
    assert cut.startswith("first-line\n")
    assert "last-line" not in cut
    assert cut.endswith(deploy_io.TRUNCATED)


def test_the_alert_excerpt_keeps_the_failing_task_ahead_of_a_long_stderr(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    """The Discord post is 700 chars of a 4000+4000 error string.

    A tail of that is stderr's deprecation warnings, not the failing task. The failing task is the part worth the budget.
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
        deploy_io.run(["sh", str(path)], cwd=str(tmp_path))
    excerpt = deploy_alert_text.alert_excerpt(excinfo.value)
    assert excerpt.startswith("sh ")
    assert TASK_HEADER in excerpt
    assert FATAL in excerpt
    assert (
        len(excerpt)
        <= deploy_alert_text.ALERT_EXCERPT_CHARS + len(deploy_io.TRUNCATED) + 1
    )


# ── a timed-out run's alert ──────────────────────────────────────────────────────────────────
# The journal keeps the whole error string, but the Discord post keeps only its HEAD: the
# broad-failure alert trims through `alert_excerpt` (ALERT_EXCERPT_CHARS) and
# host_lib.discord_post cuts at 1900 from the front. So the task that was running has to come
# FIRST in a timeout's detail, the way the failing task does in a non-zero exit's. RED proof:
# with `timeout_detail` emitting a plain tail of stdout, the header below sits ~2000 characters
# past the excerpt window and this assertion fails while the journal-level test still passes.
WEDGED_HEADER = "TASK [k8s/manifests : Apply the rendered manifests] ****"

_WEDGED_WITH_CHATTER = f"""echo '{WEDGED_HEADER}'
awk 'BEGIN {{ for (i = 0; i < 400; i++) print "changed: [daniel-box] => (item=manifest-" i ")" }}'
sleep 300
"""


def test_a_timed_out_run_puts_the_running_task_in_the_discord_excerpt(
    gitops_deploy, tmp_path: pathlib.Path
) -> None:
    path = tmp_path / "wedged.sh"
    path.write_text(_WEDGED_WITH_CHATTER)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        deploy_io.run(["sh", str(path)], cwd=str(tmp_path), timeout=1.0)

    excerpt = deploy_alert_text.alert_excerpt(excinfo.value)
    assert WEDGED_HEADER in excerpt, (
        f"the running task must survive the {deploy_alert_text.ALERT_EXCERPT_CHARS}-char "
        f"Discord excerpt; got: {excerpt!r}"
    )
    assert len(excerpt) <= deploy_alert_text.ALERT_EXCERPT_CHARS
