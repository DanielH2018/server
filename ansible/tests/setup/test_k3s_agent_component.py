"""The `k3s-agent` component as daniel-server's package renders it (H6.5a).

The package ships the files Ansible's `tasks/agent.yml` wrote, so dpkg takes each path over. It
marks each file k3s reads at startup `manual:k3s-agent.service`, and the package marks the unit
`windowed`, so reconcile never restarts the agent and the change goes live at its next start.
Every failure here is silent until that start: a drop-in that leaves stdout in the journal loses
a crash loop's reason, and a file with no activation is never reported pending.
"""

import pytest
from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "k3s-agent"
UNIT = "k3s-agent.service"
DROPIN = "/usr/lib/systemd/system/k3s-agent.service.d/50-homelab.conf"
STARTUP_FILES = {DROPIN, "/etc/rancher/k3s/registries.yaml"}
LOGROTATE = "/etc/logrotate.d/k3s-agent"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-k3s-agent.conf"


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-server")


def text(package, dst: str) -> str:
    return package.files[dst][0].decode()


def settings(content: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in content.splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.startswith("#"):
            out.setdefault(name.strip(), []).append(value.strip())
    return out


def log_file() -> str:
    return yaml_fast.safe_load((COMPONENT / "component.yml").read_text())["defaults"][
        "k3s_log_file"
    ]


def test_the_package_marks_exactly_the_startup_files_for_the_agent(package):
    marked = {
        path
        for path, action in package.activations.items()
        if action == f"manual:{UNIT}"
    }
    assert marked == STARTUP_FILES
    assert package.policies[UNIT] == "windowed"
    for quiet in (LOGROTATE, TMPFILES):
        assert quiet in package.files and quiet not in package.activations


def test_the_logging_drop_in_sends_both_streams_and_leaves_the_flags_alone(package):
    """`ExecStart` stays the installer's until H6.5b moves the agent's flags."""
    found = settings(text(package, DROPIN))
    assert found.pop("StandardOutput") == [f"append:{log_file()}"]
    assert found.pop("StandardError") == ["inherit"]
    assert found == {}


def test_rotation_truncates_in_place_because_systemd_holds_the_fd(package):
    inside = text(package, LOGROTATE).split("{", 1)[1]
    directives = {
        line.split()[0]
        for line in inside.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "}"))
    }
    assert {"copytruncate", "maxsize", "rotate"} <= directives
    assert "create" not in directives


def test_the_log_file_is_created_with_syslogs_group_at_boot(package):
    assert f"f {log_file()} 0640 root adm -" in text(package, TMPFILES)


def test_the_legacy_entry_retires_ansibles_drop_in(package):
    legacy = {"path": "/etc/systemd/system/k3s-agent.service.d/10-log-to-file.conf"}
    assert legacy in package.legacy
