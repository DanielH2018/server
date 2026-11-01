"""release-staleness-check reads a failed fetch as a skip or a retry, not as drift.

Two failures that are not drift. On 2026-10-16 the 19:00 run's `git fetch` started inside a WAN
outage, hung about two minutes, and failed just as the link came back (#5308). The cron now
probes before the fetch too, and a fetch that fails while either probe saw the link down reports
`up` with a skip note. On 2026-10-25 the 19:00 run's fetch failed on `cannot lock ref` because
the GitOps tick was fetching the same ref in the same checkout (#6816). The cron now retries
once on that error.

The real template and the real `homelab-check-lib.sh` run under bash. `curl` answers from a
link-state file, and each case's stubbed `git` says what the fetch does. The assertion is on the
`status=` `logger` line, because `probe.py alerts` rebuilds DOWN episodes from it and the
textfile carries the same verdict.
"""

from _helpers import ANSIBLE
from _shell_render import render_shell_script
from lib.proc_testing import run, write_exec

# The shared check library, minus the textfile write, which test_k3s_cron_textfiles.py covers.
CHECK_LIB = ANSIBLE / "roles/setup/initial_setup/files/homelab-check-lib.sh"
TEMPLATE = ("setup", "k3s", "release-staleness-check.sh.j2")
STATE_DIR = "/var/lib/homelab/release-staleness"
REPO_DIR = "/home/ubuntu/server"
UV = "/home/ubuntu/.local/bin/uv"
FETCH_ERR = "fatal: unable to access 'https://github.com/DanielH2018/server.git/'"
LOCK_ERR = (
    "error: cannot lock ref 'refs/remotes/origin/master': Unable to create "
    "'/home/ubuntu/server/.git/refs/remotes/origin/master.lock': File exists."
)


def _failing_git(link_after_fetch):
    """A `git` that flips the link to `link_after_fetch`, then fails the fetch unreachable."""
    return f'echo {link_after_fetch} > "$LINK"; echo "{FETCH_ERR}" >&2; return 128'


def _lock_git(lock_failures):
    """A `git` whose first `lock_failures` fetches fail on a held ref lock, then succeed."""
    return (
        'echo x >> "$CALLS"; '
        f'[[ "$(wc -l < "$CALLS")" -gt {lock_failures} ]] && return 0; '
        f'echo "{LOCK_ERR}" >&2; return 1'
    )


def _run(tmp_path, link_at_start, git_body):
    """Run the rendered cron once; return (verdicts, logger lines, git call count).

    A verdict is the run's `status=` log line without the prefix, as `<status> <message>`.
    The probe that follows a good fetch is stubbed to report a clean fleet.
    """
    body = render_shell_script(*TEMPLATE)
    for literal in ("/usr/local/lib/homelab-check-lib.sh", STATE_DIR, REPO_DIR, UV):
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"

    link = tmp_path / "link"
    link.write_text(link_at_start)
    calls = tmp_path / "git-calls"
    calls.write_text("")
    logs = tmp_path / "logs"
    logs.write_text("")
    check_lib = tmp_path / "homelab-check-lib.sh"
    check_lib.write_text(
        f"source {CHECK_LIB}\n"
        f'LINK="{link}"; CALLS="{calls}"\n'
        f'curl() {{ [[ "$(cat "{link}")" == up ]]; }}\n'
        f"git() {{ {git_body}; }}\n"
        f'logger() {{ shift 2; echo "$*" >> "{logs}"; }}\n'
        "sleep() { :; }\n"
        "write_check_textfile() { :; }\n"
    )
    uv = write_exec(
        tmp_path / "uv", "#!/usr/bin/env bash\necho 'all services current'\n"
    )
    # Redirected so a run on daniel-box, where the real directory exists, cannot write it.
    state = tmp_path / "state"
    state.mkdir()
    script = write_exec(
        tmp_path / "release-staleness-check.sh",
        body.replace("/usr/local/lib/homelab-check-lib.sh", str(check_lib))
        .replace(STATE_DIR, str(state))
        .replace(UV, str(uv))
        .replace(REPO_DIR, str(tmp_path)),
    )
    run(["bash", str(script)], check=True)
    lines = logs.read_text().splitlines()
    verdicts = [line[len("status=") :] for line in lines if line.startswith("status=")]
    return verdicts, lines, len(calls.read_text().splitlines())


def _status_lines(logs):
    return [line for line in logs if line.startswith("status=")]


def test_a_fetch_that_fails_after_the_link_returns_reads_skipped(tmp_path):
    # The 2026-10-16 sequence: down when the fetch started, back by the time it failed.
    pushes, logs, _ = _run(tmp_path, "down", _failing_git("up"))
    assert [p.split()[0] for p in pushes] == ["up"], pushes
    assert "skipped: WAN unreachable when the fetch started" in pushes[0]
    assert FETCH_ERR in pushes[0]
    assert [line.split()[0] for line in _status_lines(logs)] == ["status=up"], logs


def test_a_fetch_that_fails_with_the_link_up_throughout_reads_down(tmp_path):
    pushes, logs, _ = _run(tmp_path, "up", _failing_git("up"))
    assert [p.split()[0] for p in pushes] == ["down"], pushes
    assert "skipped" not in pushes[0]
    assert "retried" not in pushes[0]
    assert [line.split()[0] for line in _status_lines(logs)] == ["status=down"], logs
    # The banner's verdict file carries the fetch's verdict, so it reads as a broken check.
    verdict = (tmp_path / "state" / "verdict").read_text()
    assert verdict.startswith("status=down git fetch origin master failed"), verdict


def test_a_fetch_that_fails_after_the_link_drops_reads_skipped(tmp_path):
    pushes, _, _ = _run(tmp_path, "up", _failing_git("down"))
    assert [p.split()[0] for p in pushes] == ["up"], pushes
    assert "skipped: WAN unreachable — " in pushes[0]


def test_a_ref_lock_held_by_a_concurrent_fetch_is_retried_and_reads_up(tmp_path):
    # The 2026-10-25 sequence: the tick held origin/master's lock, then released it.
    pushes, _, git_calls = _run(tmp_path, "up", _lock_git(1))
    assert git_calls == 2
    assert pushes == ["up all services current"], pushes


def test_a_ref_lock_still_held_on_the_retry_reads_down(tmp_path):
    pushes, _, git_calls = _run(tmp_path, "up", _lock_git(2))
    assert git_calls == 2
    assert [p.split()[0] for p in pushes] == ["down"], pushes
    assert "retried once after a ref-lock failure" in pushes[0]
    assert "cannot lock ref" in pushes[0]
