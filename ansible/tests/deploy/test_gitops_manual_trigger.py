"""The polkit rule that lets a caller start the GitOps unit by hand stays usable and safe.

Starting a system unit by hand needs a polkit rule, because `systemctl start` goes over D-Bus
to PID 1 and is refused with "Interactive authentication required" for an unprivileged
caller. The rule must not test `subject.active` or `subject.local`, and must return YES. A
non-interactive caller — a Claude Code Bash call, a systemd-run job, a cron — has no active
local seat. An active-gated rule, or any `AUTH_*` result, matches and then still fails with
the same "Interactive authentication required" the rule exists to remove. The failure looks
identical to having no rule at all, which is what makes it worth a test.

Scope is also asserted: the rule covers this one unit and only the `start` verb. stop/restart
/kill stay privileged, because a wedged run is an incident rather than a routine action.
"""

import re
from _helpers import REPO as _REPO
from _setup_render import rendered_setup_text


_ROLE = _REPO / "ansible/roles/setup/gitops_deploy"
_RULE = "50-gitops-deploy.rules.j2"
_TASKS = _ROLE / "tasks/service.yml"

_UNIT = "gitops-deploy.service"

# Strip // and /* */ comments: the rule's header explains at length why it must NOT test
# subject.active, so a naive substring search over the whole file finds the word in the
# very prose warning against it.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _rule_code() -> str:
    text = rendered_setup_text("gitops_deploy", _RULE)
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


_UNIT_TEMPLATE = "gitops-deploy.service.j2"


def test_lock_contention_exit_code_is_one_contract_across_both_readers():
    # `flock -E N` in ExecStart and `SuccessExitStatus=N` in the unit are one contract
    # expressed in two places. If they disagree, contention pages through
    # gitops-deploy-alert.service, which never fires on an exit SuccessExitStatus makes succeed.
    unit = rendered_setup_text("gitops_deploy", _UNIT_TEMPLATE)

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


# ── this unit's own OnFailure= and TimeoutStartSec=. systemd ignores a typo'd key with a journal warning and loads the unit
# anyway, so a silent drift here would reach a host with `daemon_reload: true` reporting success.


def test_unit_pages_on_failure():
    unit = rendered_setup_text("gitops_deploy", _UNIT_TEMPLATE)
    assert re.search(r"^OnFailure=gitops-deploy-alert\.service$", unit, re.MULTILINE), (
        f"{_UNIT_TEMPLATE} no longer sets OnFailure=gitops-deploy-alert.service — a run that "
        "fails outright (not the SuccessExitStatus=75 contention case above) would page nobody."
    )


def test_unit_timeout_covers_the_broad_apply_worst_case():
    # The unit's own arithmetic comment sizes TimeoutStartSec to 40min against the broad apply's
    # 1980s. The docstring at the top of this file defers to this pin for the value.
    unit = rendered_setup_text("gitops_deploy", _UNIT_TEMPLATE)
    assert re.search(r"^TimeoutStartSec=40min$", unit, re.MULTILINE), (
        f"{_UNIT_TEMPLATE}'s TimeoutStartSec no longer reads 40min. If this is a deliberate "
        "re-size, re-derive it against the budget arithmetic in the unit's own comment and "
        "gitops_deploy/defaults/main.yml before changing this pin."
    )
