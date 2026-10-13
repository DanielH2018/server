"""pull-pi-peers.sh names a changed Pi host key, and only a changed host key.

The Secret pins the host key committed in the role, so a re-keyed Pi fails the nightly run.
ssh exits 255 for that and for an unreachable Pi alike, and rsync passes the code through, so
only ssh's `Host key verification failed.` line tells the two apart.

The whole script runs here with stubs on `PATH`. The `ssh` stub carries each case: what ssh
prints and its exit 255. leakguard shims rsync, so `rsync` is a stub too, reproducing what rsync
3.2.7 did when its `-e` ssh exited 255 (measured 2026-10-13): ssh's stderr, rsync's own two
closing lines, and exit 255 passed through. `curl` records the Kuma push.
"""

import os

import pytest
from _helpers import ROLES
from lib.proc_testing import fake_bin, path_with, run
from verdicts.logs import parse_push_line

SCRIPT_PATH = ROLES / "k8s/pi-peer-backup/files/pull-pi-peers.sh"
POD = "pi-peer-backup-29312345-x7k2q"
HOST_KEY_MESSAGE = "daniel-pi host key differs from the committed pin"

# What OpenSSH prints when StrictHostKeyChecking=yes meets a key that differs from the pin.
CHANGED_KEY = """\
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
Host key for 192.0.2.1 has changed and you have requested strict checking.
Host key verification failed."""
TIMED_OUT = "ssh: connect to host 192.0.2.1 port 22: Connection timed out"

RSYNC = """\
#!/bin/sh
ssh 192.0.2.1 rsync --server --sender
rc=$?
echo "rsync: connection unexpectedly closed (0 bytes received so far) [Receiver]" >&2
echo "rsync error: unexplained error (code $rc) at io.c(232) [Receiver=3.2.7]" >&2
exit $rc
"""


def _run_script(tmp_path, ssh_stderr: str):
    """Run the script with ssh stubbed to print `ssh_stderr` and exit 255.

    Returns the completed process and the `msg=` value of the Kuma push.
    """
    pushes = tmp_path / "curl-args"
    stubs = fake_bin(
        tmp_path / "bin",
        ssh=f"#!/bin/sh\ncat >&2 <<'EOF'\n{ssh_stderr}\nEOF\nexit 255\n",
        rsync=RSYNC,
        curl=f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {pushes}\nprintf '\\n200 text/plain'\n",
    )
    env = dict(os.environ)
    env.pop("HC_PING_URL", None)
    env |= {
        "PATH": path_with(stubs, env=env),
        "HOME": str(tmp_path / "home"),
        "HOSTNAME": POD,
        "PI_SRC": "ubuntu@192.0.2.1:/src/",
        "KUMA_PUSH_URL": "http://kuma.example/api/push/token",
    }
    result = run(["bash", str(SCRIPT_PATH)], env=env)
    msgs = [a for a in pushes.read_text().splitlines() if a.startswith("msg=")]
    assert len(msgs) == 1, msgs
    return result, msgs[0].removeprefix("msg=")


def _verdict(stdout: str):
    lines = [parse_push_line(line) for line in stdout.splitlines()]
    verdicts = [line for line in lines if line is not None]
    assert len(verdicts) == 1, stdout
    return verdicts[0]


def test_a_changed_host_key_pushes_the_named_message(tmp_path):
    result, msg = _run_script(tmp_path, CHANGED_KEY)
    assert result.returncode == 1, result.stderr
    assert msg == HOST_KEY_MESSAGE
    assert _verdict(result.stdout)[3:] == ("down", HOST_KEY_MESSAGE)


@pytest.mark.parametrize(
    "ssh_stderr",
    [TIMED_OUT, "Connection closed by 192.0.2.1 port 22"],
    ids=["timed-out", "closed"],
)
def test_an_unreachable_pi_pushes_the_rsync_error_not_the_host_key(
    tmp_path, ssh_stderr
):
    # Both exit 255 like the host-key case, so this pair proves the branch reads ssh's line.
    result, msg = _run_script(tmp_path, ssh_stderr)
    assert result.returncode == 1, result.stderr
    assert msg.startswith("rsync exit 255: "), msg
    assert HOST_KEY_MESSAGE not in msg
