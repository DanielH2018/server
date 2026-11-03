"""The `k3s-agent` component as daniel-server's package renders it (H6.5a, H6.5b).

The package ships the files Ansible's `tasks/agent.yml` wrote, so dpkg takes each path over,
and since H6.5b the agent's flags in config.yaml.d. It
marks each file k3s reads at startup `manual:k3s-agent.service`, and the package marks the unit
`windowed`, so reconcile never restarts the agent and the change goes live at its next start.
Every failure here is silent until that start: a drop-in that leaves stdout in the journal loses
a crash loop's reason, and a file with no activation is never reported pending.
"""

import pytest
from _helpers import load_tasks
from lib import yaml_fast
from lib.repo_paths import K3S_ROLE, REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "k3s-agent"
UNIT = "k3s-agent.service"
DROPIN = "/usr/lib/systemd/system/k3s-agent.service.d/50-homelab.conf"
CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"
STARTUP_FILES = {DROPIN, CONFIG, "/etc/rancher/k3s/registries.yaml"}
LOGROTATE = "/etc/logrotate.d/k3s-agent"
TMPFILES = "/usr/lib/tmpfiles.d/homelab-k3s-agent.conf"
INSTALLER = "/usr/local/bin/k3s-install.sh"


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


def test_the_drop_in_logs_both_streams_and_runs_a_bare_agent(package):
    """The bare `k3s agent` reads its flags from config.yaml.d; the env file keeps the token."""
    found = settings(text(package, DROPIN))
    assert found.pop("StandardOutput") == [f"append:{log_file()}"]
    assert found.pop("StandardError") == ["inherit"]
    assert found.pop("ExecStart") == ["", "/usr/local/bin/k3s agent"]
    assert found == {}


def test_the_config_drop_in_is_the_only_k3s_config_file(package):
    """k3s merges config.yaml and every file in config.yaml.d, so a second file adds flags."""
    configs = {
        path
        for path in package.files
        if path.startswith("/etc/rancher/k3s/config.yaml")
    }
    assert configs == {CONFIG}


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


def asserted_before_install(tasks: list[dict]) -> set[str]:
    """The paths a `stat` reads and an `assert` requires before the task that runs the installer."""
    stats: dict[str, str] = {}
    found: set[str] = set()
    for task in tasks:
        if (task.get("ansible.builtin.command") or {}).get("cmd") == INSTALLER:
            return found
        stat = task.get("ansible.builtin.stat")
        if stat and task.get("register"):
            stats[task["register"]] = stat["path"]
        that = (task.get("ansible.builtin.assert") or {}).get("that", "")
        found |= {path for reg, path in stats.items() if that == f"{reg}.stat.exists"}
    return set()


def test_the_join_refuses_an_agent_whose_package_is_not_installed(package):
    """A fresh agent that joins before its package starts with no registries.yaml."""
    guarded = asserted_before_install(load_tasks(K3S_ROLE / "tasks" / "agent.yml"))
    assert guarded & STARTUP_FILES, guarded
    assert guarded <= set(package.files)


def test_an_assert_after_the_installer_is_not_a_guard():
    tasks = [
        {"ansible.builtin.stat": {"path": "/x"}, "register": "x"},
        {"ansible.builtin.command": {"cmd": INSTALLER}},
        {"ansible.builtin.assert": {"that": "x.stat.exists"}},
    ]
    assert asserted_before_install(tasks) == set()
