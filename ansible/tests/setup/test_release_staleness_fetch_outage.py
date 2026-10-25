"""release-staleness-check reads a fetch that outlasted a WAN outage as a skip, not as drift (#5308).

On 2026-10-16 the 19:00 run's `git fetch` started inside an outage, hung about two minutes, and
failed just as the link came back. The only probe ran after the failure, saw a healthy link, and
the tile read DOWN for half an hour with no service stale. The cron now probes before the fetch
too, and a fetch that fails while either probe saw the link down reports `up` with a skip note.

The real template and the real `kuma-push-lib.sh` run under bash. `curl` answers from a link-state
file, and the stubbed `git` rewrites that file before it fails, so each case says what the link was
at the fetch's start and at its end. The assertion is on the push and on the `logger` line, because
`probe.py alerts` rebuilds DOWN episodes from `status=down` in the log.
"""

from _helpers import ANSIBLE
from _shell_render import render_shell_script
from lib.proc_testing import run, write_exec

LIB = ANSIBLE / "roles/setup/initial_setup/files/kuma-push-lib.sh"
# The shared check library, minus the textfile write, which test_k3s_cron_textfiles.py covers.
CHECK_LIB = ANSIBLE / "roles/setup/initial_setup/files/homelab-check-lib.sh"
TEMPLATE = ("setup", "k3s", "release-staleness-check.sh.j2")
FETCH_ERR = "fatal: unable to access 'https://github.com/DanielH2018/server.git/'"


def _run(tmp_path, link_at_start, link_after_fetch):
    """Run the rendered cron once with the fetch failing; return (pushes, logger lines)."""
    body = render_shell_script(*TEMPLATE)
    for literal in (
        "/usr/local/lib/kuma-push-lib.sh",
        "/usr/local/lib/homelab-check-lib.sh",
        "/etc/rancher/k3s/kuma-push.env",
    ):
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"

    link = tmp_path / "link"
    link.write_text(link_at_start)
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    logs = tmp_path / "logs"
    logs.write_text("")
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        f"source {LIB}\n"
        f'curl() {{ [[ "$(cat "{link}")" == up ]]; }}\n'
        f'git() {{ echo {link_after_fetch} > "{link}"; echo "{FETCH_ERR}" >&2; return 128; }}\n'
        f'logger() {{ shift 2; echo "$*" >> "{logs}"; }}\n'
        f'kuma_push() {{ echo "$1 $2" >> "{pushed}"; }}\n'
    )
    check_lib = tmp_path / "homelab-check-lib.sh"
    check_lib.write_text(f"source {CHECK_LIB}\nwrite_check_textfile() {{ :; }}\n")
    env = tmp_path / "kuma-push.env"
    env.write_text("RELEASE_STALENESS_PUSH_TOKEN=stubtoken\n")
    script = write_exec(
        tmp_path / "release-staleness-check.sh",
        body.replace("/usr/local/lib/kuma-push-lib.sh", str(lib))
        .replace("/usr/local/lib/homelab-check-lib.sh", str(check_lib))
        .replace("/etc/rancher/k3s/kuma-push.env", str(env)),
    )
    run(["bash", str(script)], check=True)
    return pushed.read_text().splitlines(), logs.read_text().splitlines()


def _status_lines(logs):
    return [line for line in logs if line.startswith("status=")]


def test_a_fetch_that_fails_after_the_link_returns_reads_skipped(tmp_path):
    # The 2026-10-16 sequence: down when the fetch started, back by the time it failed.
    pushes, logs = _run(tmp_path, "down", "up")
    assert [p.split()[0] for p in pushes] == ["up"], pushes
    assert "skipped: WAN unreachable when the fetch started" in pushes[0]
    assert FETCH_ERR in pushes[0]
    assert [line.split()[0] for line in _status_lines(logs)] == ["status=up"], logs


def test_a_fetch_that_fails_with_the_link_up_throughout_reads_down(tmp_path):
    pushes, logs = _run(tmp_path, "up", "up")
    assert [p.split()[0] for p in pushes] == ["down"], pushes
    assert "skipped" not in pushes[0]
    assert [line.split()[0] for line in _status_lines(logs)] == ["status=down"], logs


def test_a_fetch_that_fails_after_the_link_drops_reads_skipped(tmp_path):
    pushes, _ = _run(tmp_path, "up", "down")
    assert [p.split()[0] for p in pushes] == ["up"], pushes
    assert "skipped: WAN unreachable — " in pushes[0]
