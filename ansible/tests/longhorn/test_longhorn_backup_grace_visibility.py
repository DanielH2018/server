#!/usr/bin/env python3
"""Guards on check 4's first-run grace: a volume with NO backup must stay visible while excused.

The grace itself is correct and deliberate — a service deployed this afternoon must not redden the
backup plane before its first scheduled run. What was wrong until 2026-08-20 is that the excuse was
SILENT: the branch ended in a bare `continue`, and `CHECKED` is incremented below it, so the green
message was bit-for-bit identical whether or not the volume existed. The covered count does not
fall when a volume is graced; it fails to rise, and nobody can see a number that did not change.

That is not a hypothetical. On 2026-08-20 fourteen of twenty-five volumes were in this branch at
once — every one of them with no offsite copy anywhere — while `monitor_status == 0` returned no
data across the whole fleet. The weekly shard cadence puts a volume's first run up to six days out,
so the window is days wide, not hours.

The fix mirrors what the DISARMED path already does: count it, and name it in the green message.
Three properties are load-bearing, and a future edit could plausibly break any of them:

COUNTED. The grace branch must increment GRACED before its `continue`, or the state is invisible
again.

SURFACED. GRACED must reach MSG. A counter nothing prints is the same silence with extra steps.

NAMED. The volumes are listed, not just totalled. A graced volume has no offsite copy at all, so
"3 volume(s) awaiting" tells an operator nothing they can act on — and an unactionable red is what
check 3's own comment records as the thing that stops being read.

Run: uv run pytest ansible/tests/longhorn/test_longhorn_backup_grace_visibility.py
"""

from _helpers import ANSIBLE

HEALTH = (
    ANSIBLE / "roles" / "setup" / "k3s" / "templates" / "longhorn-backup-health.sh.j2"
)


def _code() -> str:
    """The script minus its comments — the comments discuss this bug on purpose."""
    return "\n".join(
        line
        for line in HEALTH.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _grace_branch() -> str:
    """The `if [[ -z "$LATEST" ]]` arm, up to the uncovered-reporting that follows it."""
    code = _code()
    start = code.index('if [[ -z "$LATEST" ]]')
    end = code.index("UNCOVERED=", start)
    return code[start:end]


def test_the_grace_branch_counts_rather_than_dropping_silently():
    """A graced volume increments GRACED; the bare `continue` was the whole defect."""
    branch = _grace_branch()
    assert "GRACED=$(( GRACED + 1 ))" in branch, (
        "check 4's first-run grace must count the volume it excuses — a bare `continue` "
        "leaves a volume with no backup anywhere completely invisible"
    )


def test_graced_volumes_are_named_not_just_counted():
    """The excused volumes are listed, so the message is actionable."""
    branch = _grace_branch()
    assert "GRACED_VOLS=" in branch, (
        "name the graced volumes — a bare count is not actionable, and a volume in this "
        "state has no offsite copy at all"
    )


def test_graced_reaches_the_green_message():
    """A counter that never prints is the same silence with extra steps."""
    code = _code()
    msg = next(line for line in code.splitlines() if line.strip().startswith("MSG="))
    assert "${GRACED_NOTE}" in msg, (
        "GRACED must reach MSG, the way SUPPRESSED does via DISARMED_NOTE — otherwise the "
        "green tile still reads identical while volumes sit unprotected"
    )
    assert "GRACED_NOTE=" in code and "${GRACED_VOLS}" in code, (
        "GRACED_NOTE must interpolate the named volume list"
    )


def test_checked_is_not_incremented_on_the_grace_path():
    """The regression that made this invisible: CHECKED counts only volumes with a backup.

    This is the property that makes the green count misleading on its own, and it is CORRECT —
    a graced volume genuinely is not covered. It is asserted here so that a future 'fix' that
    papers over the silence by counting graced volumes as covered fails loudly instead.
    """
    assert "CHECKED=$(( CHECKED + 1 ))" not in _grace_branch(), (
        "a volume with no backup must never count toward the covered total — that would "
        "turn an invisible gap into an actively false one"
    )


def test_both_silent_skips_are_surfaced():
    """DISARMED and GRACED are the only two paths that excuse a volume; both must be named."""
    code = _code()
    msg = next(line for line in code.splitlines() if line.strip().startswith("MSG="))
    for note in ("${DISARMED_NOTE}", "${GRACED_NOTE}"):
        assert note in msg, (
            f"{note} missing from the green message — every path that excuses a volume "
            "has to say so, or the tile reports coverage it does not have"
        )
