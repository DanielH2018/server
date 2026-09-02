"""The manual GitOps trigger keeps the two properties that make it usable and safe.

Triggering a tick by hand needs a polkit rule, because `systemctl start` on a system unit
goes over D-Bus to PID 1 and is refused with "Interactive authentication required" for an
unprivileged caller. Two things about that rule and its wrapper fail silently if edited
carelessly, so they are pinned here rather than left to review:

1. **The rule must not test `subject.active` or `subject.local`, and must return YES.** A
   non-interactive caller — a Claude Code Bash call, a systemd-run job, a cron — has no
   active local seat. An active-gated rule, or any `AUTH_*` result, matches and then still
   fails with the same "Interactive authentication required" the rule exists to remove. The
   failure looks identical to having no rule at all, which is what makes it worth a test.

2. **The wrapper must start the unit with `--no-block`.** `gitops-deploy.service` is
   Type=oneshot with TimeoutStartSec=45min, so a blocking start returns only when the whole
   tick finishes. Any caller with a shorter patience than 45 minutes — a 10-minute Bash tool
   call is the motivating one — reads that as a hang, not as a running deploy.

Scope is also asserted: the rule covers this one unit and only the `start` verb. stop/restart
/kill stay privileged, because a wedged run is an incident rather than a routine action.
"""

from __future__ import annotations

import re
from _helpers import REPO as _REPO


_ROLE = _REPO / "ansible/roles/setup/gitops_deploy"
_RULE = _ROLE / "templates/50-gitops-deploy.rules.j2"
_TASKS = _ROLE / "tasks/main.yml"
_WRAPPER = _REPO / "scripts/deploy_tools/gitops_tick.sh"

_UNIT = "gitops-deploy.service"

# Strip // and /* */ comments: the rule's header explains at length why it must NOT test
# subject.active, so a naive substring search over the whole file finds the word in the
# very prose warning against it.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _rule_code() -> str:
    text = _RULE.read_text()
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def test_rule_authorizes_without_an_interactive_session():
    code = _rule_code()
    assert "polkit.Result.YES" in code, (
        f"{_RULE} must return polkit.Result.YES — an AUTH_* result asks a caller with no "
        "active seat to authenticate interactively, which it cannot do."
    )
    for gate in ("subject.active", "subject.local"):
        assert gate not in code, (
            f"{_RULE} tests {gate}. A non-interactive caller (Bash tool, systemd-run, cron) "
            "has no active local seat, so the rule would match and still be refused."
        )
    assert "AUTH_" not in code, (
        f"{_RULE} returns an AUTH_* result; only polkit.Result.YES authorizes a caller that "
        "cannot answer an interactive prompt."
    )


def test_rule_is_scoped_to_starting_this_one_unit():
    code = _rule_code()
    assert "org.freedesktop.systemd1.manage-units" in code
    assert f'"{_UNIT}"' in code, (
        f"{_RULE} must name {_UNIT} explicitly, not a prefix or glob."
    )
    assert 'action.lookup("verb") !== "start"' in code, (
        f"{_RULE} must restrict the verb to `start`. manage-units also covers stop, restart "
        "and kill; those stay privileged."
    )


def test_rule_is_installed_where_polkit_reads_it():
    tasks = _TASKS.read_text()
    assert "50-gitops-deploy.rules.j2" in tasks, (
        f"{_RULE} exists but {_TASKS} never installs it, so no host ever gets the rule."
    )
    assert "/etc/polkit-1/rules.d/50-gitops-deploy.rules" in tasks


def test_wrapper_starts_the_unit_without_blocking():
    wrapper = _WRAPPER.read_text()
    starts = [
        line.strip()
        for line in wrapper.splitlines()
        if re.search(
            rf"systemctl start\b.*(\$UNIT|\"\$UNIT\"|{re.escape(_UNIT)})", line
        )
    ]
    assert starts, f"{_WRAPPER} no longer starts {_UNIT} at all."
    for line in starts:
        assert "--no-block" in line, (
            f"{_WRAPPER} starts the unit blocking: {line!r}. Type=oneshot plus "
            "TimeoutStartSec=45min means that returns only when the tick finishes."
        )


_UNIT_TEMPLATE = _ROLE / "templates/gitops-deploy.service.j2"


def test_lock_contention_exit_code_is_one_contract_across_all_three_readers():
    # 2026-08-23b review M1/M13. `flock -E N` in ExecStart, `SuccessExitStatus=N` in the unit,
    # and the wrapper's handler for N are one contract expressed in three places. When -E 75 and
    # SuccessExitStatus=75 landed, the wrapper was not updated: a contention tick took its
    # failure branch and told the operator that gitops-deploy-alert.service had already posted
    # to Discord. It cannot have — OnFailure never fires on a unit SuccessExitStatus makes
    # succeed. Pinning the three together is what stops the next edit re-opening that.
    unit = _UNIT_TEMPLATE.read_text()

    flock_code = re.search(r"^ExecStart=.*?flock\s+.*?-E\s+(\d+)", unit, re.MULTILINE)
    assert flock_code, (
        f"{_UNIT_TEMPLATE} ExecStart no longer passes `flock -E N`. Without it contention "
        "exits 1 and is indistinguishable from a real deploy failure."
    )
    success_code = re.search(r"^SuccessExitStatus=(\d+)", unit, re.MULTILINE)
    assert success_code, (
        f"{_UNIT_TEMPLATE} no longer sets SuccessExitStatus. Contention would page via "
        "OnFailure again — the seven false Discord alerts in the week to 2026-08-23."
    )

    code = flock_code.group(1)
    assert code == success_code.group(1), (
        f"`flock -E {code}` and `SuccessExitStatus={success_code.group(1)}` disagree. Only the "
        "exit code named by BOTH is treated as contention; a mismatch means contention pages "
        "as a failure while some unrelated exit code is silently swallowed as success."
    )
    assert code != "0", (
        "Contention must not exit 0: gitops_deploy.py's own success also exits 0, so the "
        "wrapper could not tell a skipped tick from a completed one."
    )

    stop_post = re.search(r"^ExecStopPost=.*$", unit, re.MULTILINE)
    assert stop_post, (
        f"{_UNIT_TEMPLATE} has no ExecStopPost. It is the only hook that sees $EXIT_STATUS "
        "(ExecStartPost cannot), and without it a contention tick leaves no on-host record at "
        "all: SyslogLevel/MaxLevelStore=notice drop systemd's own info-level 'Deactivated "
        "successfully' line."
    )
    assert re.search(rf'EXIT_STATUS"?\s*=\s*"?{code}\b', stop_post.group(0)), (
        f"ExecStopPost does not guard on EXIT_STATUS = {code}, so it either never fires on "
        f"contention or fires on every tick. Keep it in step with `flock -E {code}`."
    )

    # The marker string, not the exit code, is what couples the unit to the wrapper. A oneshot
    # unit with no RemainAfterExit has its ExecMainStatus reset to 0 by systemd once it goes
    # inactive (measured 2026-08-23, systemd 255.4), so `systemctl show` cannot tell a
    # contention tick from a successful deploy afterwards. Reading the exit code back was the
    # obvious fix for M1 and it would have been inert.
    marker = re.search(
        r'^CONTENTION_MARKER="([^"]+)"', _WRAPPER.read_text(), re.MULTILINE
    )
    assert marker, (
        f"{_WRAPPER} no longer defines CONTENTION_MARKER, so it cannot distinguish a skipped "
        "tick from a completed one and will report contention as a clean success."
    )
    assert marker.group(1) in stop_post.group(0), (
        f"{_WRAPPER}'s CONTENTION_MARKER {marker.group(1)!r} does not appear in the unit's "
        f"ExecStopPost line, so the wrapper greps for a phrase the unit never emits. The two "
        f"strings are one contract — change them together."
    )


def test_wrapper_stops_watching_a_joined_run_when_it_ends():
    """A tick already in flight is JOINED, not duplicated.

    The wait loop then needs a way to notice that run ending: it breaks on
    `ExecMainStartTimestampMonotonic != started_before`, and for a joined run `started_before` IS
    that run's stamp, so the loop could only exit at its deadline. land.sh sat through the full 540s
    watch cap on 2026-09-01 for a broad tick another session's merge had started, and read the
    timeout as a failure. Joining must reset the stamp so the state check alone ends the wait.
    """
    wrapper = _WRAPPER.read_text()
    join = re.search(
        r'if \[\[ "\$\(show ActiveState\)" == "activating" \]\]; then(.*?)\nelse',
        wrapper,
        re.DOTALL,
    )
    assert join, f"{_WRAPPER} no longer has a join branch for a run already in flight."
    assert re.search(r"^\s*started_before=", join.group(1), re.MULTILINE), (
        f"{_WRAPPER}'s join branch leaves started_before at the in-flight run's own stamp, "
        "so the wait loop cannot see that run finish and always runs to its deadline."
    )
