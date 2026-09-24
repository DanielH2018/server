"""The playbook run of a foreground deploy, and the annotation a finished one leaves.

`deploy_under_locks.run` calls these once it holds the service locks and has a snapshot; each
takes that run's `Run`. Split from it for length, not for a second caller.
"""

import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol

# Reach the sibling package directories: an importer that did not bootstrap them itself (a
# test, a REPL) finds only this module's own directory, and `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy_tools.exit_codes import DEPLOY_NO_HOSTS


class Run(Protocol):
    """What these read of `deploy_under_locks.Run`, which cannot be imported: it imports this."""

    repo_root: Path
    tags: list[str]
    args: list[str]
    snapshot: Path | None
    snapshot_sha: str
    owner_fd: int | None
    service_fds: list[int]

    def close(self) -> None: ...


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A PLAY RECAP host line: `<host> : ok=N ...`. Matched on `ok=` after the colon, so a host
# named anything counts and the `PLAY RECAP ****` banner, which has no colon, does not.
_RECAP_HOST = re.compile(r"^\S+\s+:\s+ok=\d+")


def recap_state(output: str) -> int:
    """0 when the PLAY RECAP names a host, 1 when it names none, 2 when there is no recap."""
    recap, hosts = False, 0
    for line in _ANSI.sub("", output).splitlines():
        if line.startswith("PLAY RECAP"):
            recap, hosts = True, 0
        elif recap and _RECAP_HOST.match(line):
            hosts += 1
    if not recap:
        return 2
    return 0 if hosts else 1


def run_playbook(run: Run) -> int:
    """Run the deploy playbook in the snapshot; its status, 78 when the recap names no host.

    UV_PROJECT_ENVIRONMENT points at the CALLING checkout's .venv: a snapshot carries none, and
    without it every deploy would build an environment inside a directory deleted minutes later.
    The service-lock and owner-lock descriptors pass to the child, so a wrapper killed with
    SIGKILL still leaves them held until the playbook itself exits, as bash's inherited
    descriptors did. SIGTERM, SIGHUP and SIGINT are forwarded to the playbook and the run waits
    for it: the snapshot must not be removed from under a playbook still reading it.

    The output is copied by a `tee` CHILD into a capture file, not by this process, for the
    reason bash piped into `tee`: the playbook's stdout reader must outlive a SIGKILLed
    wrapper. Read in process, the pipe loses its only reader with the wrapper, and the
    playbook dies of EPIPE at its next line of output with the rest of its tasks unrun.
    """
    assert run.snapshot is not None
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(run.repo_root / ".venv")}
    # Decided before the pipe: in here stdout is the pipe, and a terminal keeps its colours.
    if sys.stdout.isatty():
        env["ANSIBLE_FORCE_COLOR"] = "1"
    held = [fd for fd in (*run.service_fds, run.owner_fd) if fd is not None]
    # Outside the snapshot, which `close` removes; left behind by a SIGKILL, as bash's was.
    capture_fd, capture = tempfile.mkstemp(prefix="deploy-recap-")
    os.close(capture_fd)
    sys.stdout.flush()
    read_end, write_end = os.pipe()
    try:
        tee = subprocess.Popen(["tee", capture], stdin=read_end)
        child = subprocess.Popen(
            ["uv", "run", "ansible-playbook", "ansible/deploy.yml", *run.args],
            cwd=run.snapshot,
            env=env,
            stdout=write_end,
            pass_fds=held,
        )
    finally:
        os.close(read_end)
        os.close(write_end)
    received: list[int] = []

    def forward(signum, _frame):
        received.append(signum)
        child.send_signal(signum)

    forwarded = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    previous = {sig: signal.signal(sig, forward) for sig in forwarded}
    try:
        status = child.wait()
        tee.wait()
        output = Path(capture).read_text(errors="replace")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        Path(capture).unlink(missing_ok=True)
    if status < 0:
        status = 128 - status
    if received:
        # The run was told to stop; the playbook has exited, so cleanup is safe now.
        run.close()
        raise SystemExit(128 + received[0])
    state = recap_state(output)
    # No host under the recap: nothing ran, whatever ansible returned. A 0 with no recap is
    # the same fault; a non-zero exit with no recap keeps ansible's status, as something may
    # have applied before the run was killed or failed to parse.
    if state == 1 or (state == 2 and status == 0):
        return DEPLOY_NO_HOSTS
    return status


def annotate(run: Run) -> None:
    """Record a successful deploy where Grafana draws it as an annotation. Fire-and-forget.

    A syslog line, not a POST: this is a host process, Alloy already ships syslog to Loki, and
    Grafana reads that Loki. The sha is the snapshot's own, captured when the snapshot existed.
    """
    services = ",".join(run.tags) or "full"
    sha = run.snapshot_sha or "unknown"
    with contextlib.suppress(OSError):
        subprocess.run(
            [
                "logger",
                "-t",
                "deploy-annotation",
                f"event=deploy services={services} sha={sha} result=ok",
            ],
            stderr=subprocess.DEVNULL,
            check=False,
        )
