"""The `k3s-server` component as daniel-box's package renders it (H6.4a, H6.4b).

The package ships the files Ansible's `tasks/server.yml` wrote, so dpkg takes each path over, and
the server's flags in config.yaml.d. It marks each file k3s reads at startup `manual:k3s.service`. `homelab-k3s-restart` hashes exactly
those entries, so the next window restarts k3s once. Every failure here is silent until that
restart: a flag that names a file the package does not ship wedges the API server (the unit is
Type=notify with TimeoutStartSec=0), and a file with no activation never reaches the hash.

The renders read the tracked `secrets.yml` and daniel-box SOPS file, as
`test_host_package_two_writers.py` does, so a credential another component adds to the package
cannot refuse them.
"""

import shutil

import pytest
from _helpers import load_defaults
from lib import yaml_fast
from lib.repo_paths import K3S_ROLE, REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError

HOSTS = REPO / "hosts"
COMPONENT = HOSTS / "components" / "k3s-server"
UNIT = "k3s.service"
ACTION = f"manual:{UNIT}"
DROPIN = "/usr/lib/systemd/system/k3s.service.d/50-homelab.conf"
CONFIG_DIR = "/etc/rancher/k3s/config.yaml.d/"
CONFIG = CONFIG_DIR + "50-homelab.yaml"
# The flags whose value is a file the apiserver reads at startup and refuses to start without.
FILE_FLAGS = ("audit-policy-file", "authentication-config")
# What the restart unit hashes: every file the package ships that k3s reads at startup.
STARTUP_FILES = {
    DROPIN,
    CONFIG,
    "/var/lib/rancher/k3s/server/audit.yaml",
    "/var/lib/rancher/k3s/server/authentication-config.yaml",
    "/etc/rancher/k3s/registries.yaml",
}
MARKER = "/var/lib/rancher/k3s/server/manifests/coredns.yaml.skip"


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-box")


def text(package, dst: str) -> str:
    return package.files[dst][0].decode()


def setting(content: str, key: str) -> str | None:
    for line in content.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def component_defaults() -> dict:
    return yaml_fast.safe_load((COMPONENT / "component.yml").read_text())["defaults"]


def server_config(package) -> dict:
    """The server's flags as k3s reads them from config.yaml.d."""
    return yaml_fast.safe_load(text(package, CONFIG))


def file_flag_values(config: dict) -> dict[str, str]:
    return dict(arg.split("=", 1) for arg in config["kube-apiserver-arg"])


def test_every_file_a_k3s_flag_names_is_a_file_the_package_ships(package):
    values = file_flag_values(server_config(package))
    assert set(FILE_FLAGS) <= set(values), "the flags this test reads are gone"
    for flag in FILE_FLAGS:
        assert values[flag] in package.files, (
            f"--{flag}={values[flag]} names a file daniel-box's package does not ship, so "
            "the next k3s start finds no such file and the API server never comes up"
        )


def test_the_package_ships_the_only_file_k3s_reads_its_config_from(package):
    """k3s merges config.yaml and every file in config.yaml.d, so a second file adds flags."""
    in_dir = sorted(path for path in package.files if path.startswith(CONFIG_DIR))
    assert in_dir == [CONFIG]
    assert "/etc/rancher/k3s/config.yaml" not in package.files


def test_the_unit_drop_in_replaces_the_installers_command_line(package):
    """The empty `ExecStart=` must come first, or systemd refuses a second ExecStart."""
    exec_lines = [
        line.partition("=")[2].strip()
        for line in text(package, DROPIN).splitlines()
        if line.startswith("ExecStart=")
    ]
    assert exec_lines == ["", "/usr/local/bin/k3s server"]


COPIED_KEYS = ("k3s_log_file",)


def drifted(component: dict, role: dict) -> list[str]:
    """The copied keys whose value in the component differs from setup/k3s's twin."""
    return [key for key in COPIED_KEYS if component.get(key) != role.get(key)]


def test_each_copied_key_equals_its_setup_k3s_twin():
    assert drifted(component_defaults(), load_defaults(K3S_ROLE)) == []


def test_a_copy_that_drifts_from_its_twin_is_detected():
    role = load_defaults(K3S_ROLE)
    assert drifted(
        {**component_defaults(), "k3s_log_file": "/var/log/other.log"}, role
    ) == ["k3s_log_file"]


def test_the_package_marks_exactly_the_startup_files_for_the_k3s_restart(package):
    marked = {path for path, action in package.activations.items() if action == ACTION}
    assert marked == STARTUP_FILES
    assert package.policies[UNIT] == "windowed"
    for quiet in ("/etc/logrotate.d/k3s", MARKER):
        assert quiet in package.files and quiet not in package.activations


def test_the_logging_drop_in_sends_both_streams_to_the_log_file(package):
    content = text(package, DROPIN)
    log_file = load_defaults(K3S_ROLE)["k3s_log_file"]
    assert setting(content, "StandardOutput") == f"append:{log_file}"
    assert setting(content, "StandardError") == "inherit"


def test_rotation_truncates_in_place_because_systemd_holds_the_fd(package):
    inside = text(package, "/etc/logrotate.d/k3s").split("{", 1)[1]
    directives = {
        line.split()[0]
        for line in inside.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "}"))
    }
    assert {"copytruncate", "maxsize", "rotate"} <= directives
    assert "create" not in directives


def test_the_legacy_entry_retires_ansibles_drop_in(package):
    legacy = {"path": "/etc/systemd/system/k3s.service.d/10-log-to-file.conf"}
    assert legacy in package.legacy
    spec = yaml_fast.safe_load((COMPONENT / "component.yml").read_text())
    assert spec["legacy"] == [legacy]


def test_the_coredns_marker_ships_empty(package):
    assert text(package, MARKER) == ""


def test_the_audit_log_directory_and_log_file_are_created_at_boot(package):
    tmpfiles = text(package, "/usr/lib/tmpfiles.d/homelab-k3s-server.conf")
    assert "f /var/log/k3s.log 0640 root adm -" in tmpfiles
    assert "d /var/lib/rancher/k3s/server/logs 0700 root root -" in tmpfiles


def test_a_template_naming_an_undefined_key_fails_the_host_render(tmp_path):
    hosts = tmp_path / "hosts"
    shutil.copytree(HOSTS, hosts)
    # Sources outside hosts/ resolve against the root's parent.
    for name in ("ansible", "scripts", "docs"):
        (tmp_path / name).symlink_to(REPO / name)
    host_mode.render_host(
        "daniel-box", hosts_root=hosts
    )  # the accept half: the copy renders as it stands
    broken = hosts / "components/k3s-server/templates/50-homelab.conf.j2"
    broken.write_text("[Service]\nEnvironment=X={{ k3s_no_such_key }}\n")
    with pytest.raises(RenderError, match="k3s_no_such_key"):
        host_mode.render_host("daniel-box", hosts_root=hosts)
