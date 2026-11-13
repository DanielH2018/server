"""daniel-pi's audit-log heartbeat reports an auditd that is active and writes nothing.

Each case renders pi-audit-health through `_pi_health.render` and runs it against a stub
`systemctl` and a temp audit.log whose last write the case sets.

Run: uv run pytest hosts/tests/test_pi_audit_health.py
"""

import os
from pathlib import Path

import pytest
from _pi_health import render, textfile
from lib.proc_testing import fake_bin, run

AUDIT_LOG = "/var/log/audit/audit.log"
# `systemctl is-active`: the state on stdout, and exit 0 only for active.
SYSTEMCTL_STUB = """\
#!/usr/bin/env bash
echo "$STUB_AUDITD_STATE"
[ "$STUB_AUDITD_STATE" = active ]
"""


def verdict(
    tmp_path: Path, state: str, written_min_ago: int | None
) -> tuple[float, str]:
    """Run the heartbeat against one auditd state and one audit.log.

    Args:
        tmp_path: Where the script, its stubs and its outputs go.
        state: What `systemctl is-active auditd.service` prints.
        written_min_ago: Minutes since audit.log's last write, or None for no log at all.

    Returns:
        The `homelab_check_success` value the run wrote, and its health-log line.
    """
    script, health_log = render("pi-audit-health", tmp_path)
    audit_log = tmp_path / "audit.log"
    if written_min_ago is not None:
        audit_log.write_text("type=DAEMON_START\n")
        written = audit_log.stat().st_mtime - written_min_ago * 60
        os.utime(audit_log, (written, written))
    body = script.read_text()
    assert AUDIT_LOG in body, (
        "the script no longer reads the audit log this test repoints"
    )
    script.write_text(body.replace(AUDIT_LOG, str(audit_log)))
    (tmp_path / "textfile").mkdir()
    run(
        ["bash", str(script)],
        check=True,
        stub_bin=fake_bin(tmp_path / "bin", systemctl=SYSTEMCTL_STUB),
        env={
            "PATH": "/usr/bin:/bin",
            "STUB_AUDITD_STATE": state,
            "TEXTFILE_DIR": str(tmp_path / "textfile"),
        },
    )
    success = textfile(tmp_path, "pi-audit-health")["homelab_check_success"]
    return success, health_log.read_text()


@pytest.mark.parametrize(
    ("state", "written_min_ago", "success", "says"),
    [
        ("active", 5, 1, "status=up"),
        # The 2026-11-13 state: the unit active, and the log silent since the last boot.
        ("active", 90, 0, "no write for 60 minutes"),
        ("inactive", 5, 0, "auditd.service is inactive"),
        ("active", None, 0, "cannot read"),
    ],
    ids=["writing", "suspended", "stopped", "no-log"],
)
def test_the_heartbeat_reports_whether_auditd_is_writing_its_log(
    tmp_path, state, written_min_ago, success, says
):
    reported, line = verdict(tmp_path, state, written_min_ago)

    assert reported == success
    assert says in line
