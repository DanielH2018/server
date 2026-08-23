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
_WRAPPER = _REPO / "scripts/gitops_tick.sh"

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
