"""k3s's own output survives the journald cap, on both node types.

`MaxLevelStore=notice` (initial_setup/tasks/system-tuning.yml) drops every priority-info line
at the source, and every line k3s writes is priority info: logrus and klog emit plain text with
no `<N>` prefix, so the unit's `SyslogLevel=info` default applies to INFO, WARN and FATA alike.
On 2026-09-09 daniel-box's k3s crash-looped ~3000 times over 5h08m and the journal kept only
systemd's `status=1/FAILURE` lines; why k3s exited is unrecoverable (#1918).

tasks/unit-logging.yml sends the unit's stdout and stderr to a logrotated file through a
drop-in. Every failure here is silent — the unit runs, the journal looks as it always did, and
the next crash loop is the first anyone learns the file stopped being written.
"""

import pytest
from lib import yaml_fast
from _helpers import SETUP_ROLES, walk_tasks

ROLE = SETUP_ROLES / "k3s"
UNIT_LOGGING = ROLE / "tasks" / "unit-logging.yml"
DEFAULTS = yaml_fast.safe_load((ROLE / "defaults" / "main.yml").read_text())
LOG_FILE = DEFAULTS["k3s_log_file"]

# The unit each node type runs, and the task file that must import the drop-in for it. The
# installer rewrites /etc/systemd/system/<unit>.service wholesale, so the drop-in is the only
# placement that survives — and the agent is a separate host, so fixing the server alone is the
# entry-points failure: a daniel-server crash loop would be exactly as unrecoverable.
UNITS = {
    "k3s": ROLE / "tasks" / "server.yml",
    "k3s-agent": ROLE / "tasks" / "agent.yml",
}

SYSTEM_TUNING = SETUP_ROLES / "initial_setup" / "tasks" / "system-tuning.yml"
JOURNALD_DEST = "/etc/systemd/journald.conf.d/50-homelab.conf"


def copy_tasks(tasks) -> dict[str, str]:
    """`dest` -> `content` for every ansible.builtin.copy task, wrappers descended."""
    out = {}
    for task in walk_tasks(tasks):
        copy = task.get("ansible.builtin.copy")
        if isinstance(copy, dict) and "dest" in copy:
            out[copy["dest"]] = copy.get("content", "")
    return out


def unit_setting(content: str, key: str) -> str | None:
    for line in content.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def logrotate_directives(content: str) -> set[str]:
    """The bare directives inside the `{ ... }` block, comments dropped."""
    inside = content.split("{", 1)[1].split("}", 1)[0] if "{" in content else ""
    return {
        line.split()[0]
        for line in inside.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def importing_unit(path) -> str | None:
    """The `k3s_log_unit` a task file hands to unit-logging.yml, or None if it never imports it."""
    for task in walk_tasks(yaml_fast.safe_load(path.read_text())):
        if task.get("ansible.builtin.import_tasks") == "unit-logging.yml":
            return (task.get("vars") or {}).get("k3s_log_unit")
    return None


def rendered(unit: str) -> dict[str, str]:
    """unit-logging.yml's copy tasks with the two variables it takes substituted."""
    raw = (
        UNIT_LOGGING.read_text()
        .replace("{{ k3s_log_unit }}", unit)
        .replace("{{ k3s_log_file }}", LOG_FILE)
    )
    return copy_tasks(yaml_fast.safe_load(raw))


def test_the_journal_still_drops_info_so_the_file_is_load_bearing():
    """If this ever moves to info the file becomes belt-and-braces, not the only copy."""
    tuning = copy_tasks(yaml_fast.safe_load(SYSTEM_TUNING.read_text()))
    assert unit_setting(tuning[JOURNALD_DEST], "MaxLevelStore") == "notice"


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_each_node_type_imports_the_drop_in_for_its_own_unit(unit: str):
    assert importing_unit(UNITS[unit]) == unit, (
        f"{UNITS[unit].name} does not import unit-logging.yml with k3s_log_unit={unit}, so "
        f"that node's k3s output is dropped by MaxLevelStore=notice and a crash loop leaves "
        f"no reason behind"
    )


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_drop_in_sends_both_streams_to_the_file(unit: str):
    dropin = rendered(unit)[f"/etc/systemd/system/{unit}.service.d/10-log-to-file.conf"]
    assert unit_setting(dropin, "StandardOutput") == f"append:{LOG_FILE}", (
        "StandardOutput is not `append:<k3s_log_file>` — the journald cap drops the stream"
    )
    assert unit_setting(dropin, "StandardError") in ("inherit", f"append:{LOG_FILE}"), (
        "stderr is not routed to the same file; logrus and klog both write to stderr, so "
        "this is the stream the crash reason is on"
    )


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_rotation_truncates_in_place_because_systemd_holds_the_fd(unit: str):
    directives = logrotate_directives(rendered(unit)[f"/etc/logrotate.d/{unit}"])
    assert "copytruncate" in directives, (
        "without copytruncate a rename leaves the unit writing to the rotated inode until "
        "its next restart, and the live file stays empty"
    )
    assert "create" not in directives, (
        "`create` alongside copytruncate is a contradiction"
    )
    assert "maxsize" in directives and "rotate" in directives, (
        "the crash-loop shape (~3000 restarts in 5h on 2026-09-09) needs a size bound as well "
        "as a history bound"
    )


# ── The rejecting halves ──────────────────────────────────────────────────────────────────


def test_a_drop_in_that_leaves_stdout_in_the_journal_is_detected():
    assert unit_setting("[Service]\nStandardError=inherit\n", "StandardOutput") is None


def test_a_task_file_that_never_imports_the_drop_in_is_detected():
    assert importing_unit(SYSTEM_TUNING) is None


def test_a_rename_rotation_is_detected():
    stanza = "/var/log/x.log {\n  daily\n  rotate 4\n  create 0640 root adm\n}\n"
    directives = logrotate_directives(stanza)
    assert "copytruncate" not in directives and "create" in directives
