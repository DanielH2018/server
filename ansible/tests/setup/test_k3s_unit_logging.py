"""k3s's own output needs its log file, because the journal keeps none of it.

`MaxLevelStore=notice` (initial_setup/tasks/system-tuning.yml) drops every priority-info line
at the source, and every line k3s writes is priority info: logrus and klog emit plain text with
no `<N>` prefix, so the unit's `SyslogLevel=info` default applies to INFO, WARN and FATA alike.
A k3s crash loop therefore leaves only systemd's `status=1/FAILURE` lines in the journal.

The drop-ins that send each unit's output to a logrotated file are the host packages' since
H6.4a (server) and H6.5a (agent). `test_k3s_server_component.py` and
`test_k3s_agent_component.py` hold them.
"""

from lib import yaml_fast
from _helpers import SETUP_ROLES, walk_tasks

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


def test_the_journal_still_drops_info_so_the_file_is_load_bearing():
    """If this ever moves to info the file becomes belt-and-braces, not the only copy."""
    tuning = copy_tasks(yaml_fast.safe_load(SYSTEM_TUNING.read_text()))
    assert unit_setting(tuning[JOURNALD_DEST], "MaxLevelStore") == "notice"
