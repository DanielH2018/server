"""k3s's own output needs its log file, because the journal keeps none of it.

`MaxLevelStore=notice` (the host-tuning component's journald drop-in) drops every priority-info
line at the source, and every line k3s writes is priority info: logrus and klog emit plain text
with no `<N>` prefix, so the unit's `SyslogLevel=info` default applies to INFO, WARN and FATA
alike. A k3s crash loop therefore leaves only systemd's `status=1/FAILURE` lines in the journal.

The drop-ins that send each unit's output to a logrotated file are the host packages' since
H6.4a (server) and H6.5a (agent). `test_k3s_server_component.py` and
`test_k3s_agent_component.py` hold them.
"""

import pytest
from _host_packages import HOSTS, shipped_text

# The host-tuning component ships this drop-in in every host package.
JOURNALD_DEST = "/etc/systemd/journald.conf.d/50-homelab.conf"


def unit_setting(content: str, key: str) -> str | None:
    for line in content.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


@pytest.mark.parametrize("host", HOSTS)
def test_the_journal_still_drops_info_so_the_file_is_load_bearing(host: str):
    """If this ever moves to info the file becomes belt-and-braces, not the only copy."""
    assert unit_setting(shipped_text(host, JOURNALD_DEST), "MaxLevelStore") == "notice"
