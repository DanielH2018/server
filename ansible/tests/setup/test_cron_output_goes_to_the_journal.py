"""The crons that print on a successful run send that output to the journal, not to mail.

cron mails whatever a job writes to /var/mail/ubuntu, and nothing opens that spool. A per-run
success line there is not a record — it is one more message a real failure has to be found
among. #2418 silenced the infra-map cron's, #2444 drained the spool, and #2466 is the six that
still mailed: four crons, plus `secret-rotation-audit` and `live-drift`, which the kuma-check
timer migration had already moved to the journal by passing `kuma_check_cron_name`.

Each of the four routes BOTH streams through `logger -t <tag>`. Both, not stdout alone: the
cert-expiry line this exists to silence comes from `logging.basicConfig`, which writes to
stderr. None of the four loses an alert, because none of them alerted by mail — the reasoning
per cron is at its own task.

Run: uv run pytest ansible/tests/setup/test_cron_output_goes_to_the_journal.py
"""

import re

import pytest
from _helpers import SETUP_ROLES, load_tasks, walk_tasks

CRON_FILES = (
    SETUP_ROLES / "initial_setup" / "tasks" / "crons.yml",
    SETUP_ROLES / "k3s" / "tasks" / "health-crons.yml",
)

# The four crons of #2466 that mail a per-run success line, by `ansible.builtin.cron` name, and
# the `logger` tag each must route to. Named rather than globbed: a census that finds its
# subject by pattern reads empty the day a file moves, and every job then passes by default.
# A rename fails `test_every_named_cron_still_exists` below rather than going quiet.
JOURNAL_ROUTED = {
    "Weekly secret rotation (auto tier)": "secret-rotate",
    "TLS cert-expiry watch": "cert-expiry",
    "Refresh generated docs": "docs-refresh",
    "Longhorn filesystem trim": "longhorn-trim-cron",
    # Not one of the four — added with the branch sweep (#2430), which made a previously
    # near-silent `--gc` job print a line per removed worktree and deleted branch.
    "Weekly git object-store repair": "worktree-sweep",
}


# `2>&1 | logger -t <tag>`, allowing the shell's own spacing. Matched as written because the
# redirect is the whole fix: `| logger` without `2>&1` leaves stderr mailing, which is exactly
# the cert-expiry case.
def _routing(tag: str) -> re.Pattern[str]:
    return re.compile(r"2>&1\s*\|\s*logger\s+-t\s+" + re.escape(tag) + r"\b")


def _cron_jobs() -> dict[str, str]:
    """{cron name: job string} for every `ansible.builtin.cron` task that installs a job."""
    jobs: dict[str, str] = {}
    for path in CRON_FILES:
        for task in walk_tasks(load_tasks(path)):
            cron = task.get("ansible.builtin.cron")
            if isinstance(cron, dict) and cron.get("job"):
                jobs[str(cron["name"])] = str(cron["job"])
    return jobs


def test_every_named_cron_still_exists():
    """The census names four crons; all four are installed by the task files it reads."""
    assert set(JOURNAL_ROUTED) <= set(_cron_jobs())


@pytest.mark.parametrize(("name", "tag"), sorted(JOURNAL_ROUTED.items()))
def test_the_cron_sends_both_streams_to_the_journal(name, tag):
    job = _cron_jobs()[name]
    assert _routing(tag).search(job), f"{name!r} still mails its output: {job}"


def test_a_stdout_only_redirect_does_not_satisfy_the_check():
    """The RED half: `| logger` without `2>&1` is the bug, not the fix."""
    assert not _routing("cert-expiry").search(
        "uv run python scripts/watchers/cert_expiry.py | logger -t cert-expiry"
    )


def test_the_braced_chain_pipes_the_whole_cert_expiry_job():
    """`|` binds tighter than `&&`, so an unbraced chain would pipe only its last stage.

    The cert-expiry job is the only one of the four that is a chain rather than one command,
    and it is the one where getting this wrong reads as fixed while the `cd` and the
    credentials-file stage keep mailing.
    """
    job = _cron_jobs()["TLS cert-expiry watch"]
    assert job.startswith("{ cd "), job
    assert "; } 2>&1 | logger -t cert-expiry" in job, job
