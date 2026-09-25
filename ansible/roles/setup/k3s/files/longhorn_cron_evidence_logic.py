"""Checks 9 and 10 of the Longhorn backup-plane heartbeat — the two crons' evidence plane.

Split out of longhorn_backup_health_logic.py when that module reached its length cap, and the
split is also the shape of the thing: checks 1-8 read the CLUSTER, these two read a journal and
a cron file. Same contract as the module they came from — stdlib only, every clock reading and
file read injected, no cluster needed to test it.

Check 9 reads what the `longhorn-trim` and `b2-deletions` crons SAID (#2418). Check 10 reads
whether they said anything at all (#2443). They are separate arms on one journal read because
silence and a bad verdict are different faults with different fixes.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Resolves via longhorn_backup_health.py's own sys.path.insert, exactly as the sibling logic
# module's does.
import host_lib

# ── check 9: do the trim and B2-accounting crons have a reader? ─────────────────────────────

# `trimmed 41 volume(s), 3 skipped, 0 failed` — longhorn-trim-volumes.sh's unconditional summary
# line, matched as written rather than by field position. The abort arm logs no summary at all,
# which is why both shapes are searched and the newest of either decides.
_TRIM_SUMMARY_RE = re.compile(r"trimmed \d+ volume\(s\), \d+ skipped, (\d+) failed")
_TRIM_ABORT_RE = re.compile(r"^ABORT: (.*)")


def _newest_trim_verdict(lines: list[str]) -> tuple[int, str] | None:
    """The trim's last run, read backwards from the newest journal line.

    Returns None when no line in the window carries either shape. That is "no reading", not
    "healthy": whether the trim cron is still running at all is check 10's alarm, which reads
    the same lines through `trim_has_spoken` and does claim it (#2443).
    """
    for line in reversed(lines):
        text = line.strip()
        abort = _TRIM_ABORT_RE.match(text)
        if abort:
            return (4, f"longhorn-trim aborted its last run: {abort.group(1)[:160]}")
        summary = _TRIM_SUMMARY_RE.search(text)
        if summary:
            failed = int(summary.group(1))
            if failed:
                return (
                    4,
                    f"longhorn-trim failed on {failed} volume(s) on its last run — "
                    "freed blocks are still being backed up",
                )
            return None
    return None


def check_cron_evidence(
    trim_lines: list[str] | None,
    deletion_lines: list[str] | None,
    window_hours: int,
) -> list[tuple[int, str]]:
    """Whether the trim and B2-deletion-accounting crons reported anything worth a page.

    Both crons write their verdict through `logger` and nothing read it (#2418). The trim's own
    comment claimed "a cron mail or a Kuma push notices" a failure; no Kuma push existed, and the
    cron mail lands in /var/mail/ubuntu among thousands of unread success lines. This check is
    that reader.

    `trim_lines` / `deletion_lines` are the journal lines for each tag over the last
    `window_hours`, or None when the read itself failed.

    Rank 4 throughout, where `_fetch_text` uses rank 1 for a failed kubectl read. That asymmetry
    is deliberate: a kubectl failure means the backup plane is unreadable, while everything here
    is the evidence plane. A trim that reclaimed nothing and a deletion nobody could price are
    both real and neither is urgent, so they must not displace a stale backup from the push slot.
    """
    problems: list[tuple[int, str]] = []

    if trim_lines is None:
        problems.append(
            (
                4,
                "could not read the longhorn-trim journal — a trim failure has no reader",
            )
        )
    else:
        trim_problem = _newest_trim_verdict(trim_lines)
        if trim_problem:
            problems.append(trim_problem)

    if deletion_lines is None:
        problems.append(
            (
                4,
                "could not read the b2-deletions journal — an UNPRICED deletion has no reader",
            )
        )
    else:
        # The whole window is searched, not just the newest run, for two reasons. `logger` writes
        # one journal entry per line and probe.py's UNPRICED report is a multi-line block, so
        # "the newest run" cannot be reconstructed from the lines alone. And an unpriced deletion
        # is a permanent EVENT rather than a current state — the transactions it spent cannot be
        # recovered by any later run — so the right behaviour is to report it once and let it age
        # out of the window, which it does on its own.
        unpriced = [line for line in deletion_lines if "UNPRICED" in line]
        if unpriced:
            problems.append(
                (
                    4,
                    f"b2-deletions reported UNPRICED in the last {window_hours}h "
                    f"(Class C spent that no later run can price): {unpriced[0].strip()[:160]}",
                )
            )

    return problems


# ── check 10: are those two crons still firing at all? ──────────────────────────────────────

# One period of either cron. `health-crons.yml` schedules both with an hour and a minute and NO
# `weekday`, so each fires once a day. If either gains a weekday, this constant and the window
# guard in `check_cron_liveness` change together — a weekly cron is legitimately silent for six
# days and the guard below would have to compare against its own period instead.
_CRON_PERIOD_S = 86400

# `b2-deletions: charged 3, skipped 0, unpriced 0` and `b2-deletions: declined: ...` —
# `deletions_summary_line` and `deletions_declined_line` in
# `scripts/diagnostics/probe_lib/b2_ledger.py`, matched as written the way the trim's summary
# is. A completed run prints exactly one of them.
_DELETIONS_SUMMARY_RE = re.compile(
    r"b2-deletions: charged \d+, skipped \d+, unpriced \d+"
)
_DELETIONS_DECLINED_RE = re.compile(r"b2-deletions: declined: ")
# The two unconditional shapes that run printed BEFORE it gained a summary line. They are
# accepted so the first window after the change — which still holds runs from before it — does
# not read as a stopped cron and page for a day.
# TODO: https://github.com/DanielH2018/server/issues/2565 - drop these once one full evidence
# window has passed with the summary line in it.
_DELETIONS_LEGACY_RE = re.compile(
    r"no new B2 backup deletions in the last |charged \d+ deletion\(s\) over "
)


class CronState(NamedTuple):
    """What the host knows about one of check 9's crons, apart from what it logged.

    Attributes:
        tag: the syslog tag the cron's verdict lines carry. Named in the message because it is
            how an operator finds the cron again (`journalctl -t <tag>`).
        path: the `/etc/cron.d` file Ansible installs the cron as.
        installed_at: that file's mtime, or None when the file does not exist.
        unreadable: the file exists but could not be stat'd — a permissions change, most
            likely. Distinct from a missing file for the reason `check_restore_drill`'s
            `stamp_unreadable` flag is distinct: "the cron is gone" and "I could not look" have
            different fixes, and folding them sends whoever is paged to the wrong one.
        expected: whether this host installs the cron at all. The B2-accounting cron is gated
            on `has_repo_checkout` and a host without a checkout never gets one; the trim cron
            carries no such gate.
    """

    tag: str
    path: str
    installed_at: float | None
    unreadable: bool
    expected: bool


def deletions_have_spoken(lines: list[str]) -> bool:
    """Whether the window holds a line a COMPLETED `b2-deletions` run writes.

    `probe.py b2-deletions` ends every run that completes with one of two fixed shapes —
    `deletions_summary_line` for a run that classified, `deletions_declined_line` for a
    disarmed backup target (`scripts/diagnostics/probe_lib/b2_ledger.py`). Judging on those
    rather than on any line at all is #2545: the cron pipes both streams through `logger`, so a
    run that logged a traceback and exited non-zero used to read as alive here, and check 9
    stayed quiet too because a traceback carries no `UNPRICED`.
    """
    return any(
        _DELETIONS_SUMMARY_RE.search(line)
        or _DELETIONS_DECLINED_RE.search(line)
        or _DELETIONS_LEGACY_RE.search(line)
        for line in lines
    )


def trim_has_spoken(lines: list[str]) -> bool:
    """Whether the window holds a line `_newest_trim_verdict` recognises.

    Check 9 reads the trim's CONTENT and returns None both for a clean run and for a window
    that held no run at all. This separates the two, so check 10 can alarm on the second
    without check 9 having to claim anything about liveness.
    """
    return any(
        _TRIM_ABORT_RE.match(line.strip()) or _TRIM_SUMMARY_RE.search(line.strip())
        for line in lines
    )


def _liveness_problem(
    cron: CronState,
    lines: list[str] | None,
    spoke: bool,
    window_hours: int,
    now_s: float,
) -> tuple[int, str] | None:
    """Whether `cron` has gone silent, given what its tag logged inside the window."""
    if not cron.expected or spoke:
        return None
    if lines is None:
        # Check 9 already reports the failed journal read, and it is the same fault. A second
        # problem off one unreadable journal would double-count it into the push slot.
        return None
    if cron.unreadable:
        return (
            4,
            f"could not stat {cron.path} — whether the {cron.tag} cron is still "
            "installed has no reader",
        )
    if cron.installed_at is None:
        return (
            4,
            f"the {cron.tag} cron is not installed ({cron.path}) and nothing ran it in "
            f"the last {window_hours}h",
        )
    if now_s - cron.installed_at < window_hours * 3600:
        # A freshly provisioned host, or one whose cron Ansible has just rewritten. It has not
        # had a full window to fire in, so its silence proves nothing. The window is the grace
        # because the window is already one period plus slack — no second tunable.
        return None
    return (
        4,
        f"{cron.tag} has logged nothing in the last {window_hours}h — its cron has "
        f"stopped firing ({cron.path})",
    )


def check_cron_liveness(
    trim_lines: list[str] | None,
    deletion_lines: list[str] | None,
    trim: CronState,
    deletion: CronState,
    window_hours: int,
    now_s: float,
) -> list[tuple[int, str]]:
    """Whether the trim and B2-accounting crons still fire, as distinct from what they said.

    Check 9 reads both crons' CONTENT and stays green on an empty window, which is the same
    shape the restore drill's "a drill that silently stops looks identical to one never
    scheduled" had before check 7 closed it (#2443). A trim cron whose entry is removed, whose
    script goes un-rendered, or whose crontab is lost leaves the window empty forever and check
    9 green forever. This arm is the reader for that.

    Silence alone is not the alarm — three things legitimately produce it, and each is answered
    without a new tunable:

    - A window shorter than one cron period. Then silence is the normal reading on most ticks,
      so the arm does not run at all.
    - A cron this host does not install (`CronState.expected`).
    - A cron installed less than one window ago: a freshly provisioned host, or one Ansible has
      just rewritten. The window is already one period plus slack, so it is the grace too.

    Both arms judge silence on a RECOGNISED line, so each proves its cron COMPLETED rather
    than merely fired. The b2 half judged on any line at all until #2545 gave
    `probe.py b2-deletions` a summary line of its own: the cron pipes both streams through
    `logger`, so a traceback was evidence of life, and check 9 stayed quiet alongside it
    because a traceback carries no `UNPRICED`.

    Rank 4 throughout, matching check 9: a cron that stopped is real and not urgent, and it
    must not displace a stale backup from the push slot.
    """
    if window_hours * 3600 < _CRON_PERIOD_S:
        return []
    problems = []
    for cron, lines, spoke in (
        (trim, trim_lines, bool(trim_lines) and trim_has_spoken(trim_lines)),
        (
            deletion,
            deletion_lines,
            bool(deletion_lines) and deletions_have_spoken(deletion_lines),
        ),
    ):
        problem = _liveness_problem(cron, lines, spoke, window_hours, now_s)
        if problem:
            problems.append(problem)
    return problems


def cron_state(tag: str, path: str, expected: bool) -> CronState:
    """`CronState` for the cron installed at `path`, reading its install time off the file."""
    installed_at, unreadable = host_lib.file_mtime(path)
    return CronState(tag, path, installed_at, unreadable, expected)


def check(
    journal,
    window_hours: int,
    trim: CronState,
    deletion: CronState,
    now_s: float,
) -> list[tuple[int, str]]:
    """Checks 9 and 10 over ONE journal read each, in the order they are reported.

    `journal(tag)` is `host_lib.journal_reader`'s bound reader: the lines that tag logged in
    the window, or None when the read itself failed. The two arms share the read because they
    are two questions about the same lines — what the cron said, and whether it said anything.
    """
    trim_lines = journal(trim.tag)
    deletion_lines = journal(deletion.tag)
    return check_cron_evidence(
        trim_lines, deletion_lines, window_hours
    ) + check_cron_liveness(
        trim_lines, deletion_lines, trim, deletion, window_hours, now_s
    )
