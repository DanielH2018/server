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

import copy
import shutil

import pytest
from _helpers import registry_secret_names
from lib import yaml_fast
from lib.repo_paths import K3S_SERVER, REPO
from longhorn_lib import longhorn_etcd_s3_logic as etcd_s3
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError

HOSTS = REPO / "hosts"
COMPONENT = K3S_SERVER
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


def test_the_config_drop_in_ships_world_readable_in_a_directory_nfpm_creates(package):
    """Check 12 (H2.8b) reads `etcd-s3` from the drop-in as `sys_user`.

    nfpm creates a parent directory the package does not list at 0755, so the directory stays
    readable as long as no entry declares it with another mode.
    """
    assert package.files[CONFIG][1] == 0o644
    declared = yaml_fast.safe_load(package.nfpm_spec())["contents"]
    assert [e for e in declared if e["dst"].rstrip("/") + "/" == CONFIG_DIR] == []


def test_the_drop_in_names_only_the_s3_keys_that_defer_to_the_secret(package):
    """Any other `etcd-s3-*` flag makes k3s ignore `etcd-s3-config-secret` (H2.8a)."""
    config = server_config(package)
    assert config["etcd-s3"] is True
    assert config["etcd-s3-config-secret"] == "k3s-etcd-snapshot-s3-config"
    assert sorted(key for key in config if key.startswith("etcd-s3")) == [
        "etcd-s3",
        "etcd-s3-config-secret",
    ]


def test_check_12_reads_the_drop_in_the_package_renders(package):
    """Backup health parses this file without YAML, so it must meet the real render."""
    assert etcd_s3.allowed_age_s(lambda: text(package, CONFIG)) == 14 * 3600


def placeholder_leaks(package, names: list[str]) -> list[str]:
    """Files outside the package's declared secret outputs that carry a `${<secret>}`."""
    outputs = {output["template"] for output in package.outputs}
    return sorted(
        f"{dst} names ${{{name}}}"
        for dst, (content, _) in package.files.items()
        if dst not in outputs
        for name in names
        if f"${{{name}}}" in content.decode(errors="replace")
    )


def test_no_file_outside_the_secret_outputs_carries_a_secret_placeholder(package):
    """The template-variable half is the render's own refusal, tested below."""
    names = registry_secret_names()
    assert CONFIG in package.files
    assert package.outputs, (
        "daniel-box declares secret outputs; the exemption read none"
    )
    assert placeholder_leaks(package, names) == []


def test_a_secret_placeholder_in_the_drop_in_is_flagged(package):
    leaky = copy.copy(package)
    leaky.files = dict(package.files)
    leaky.files[CONFIG] = (b"etcd-s3-access-key: ${r2_access_key_id}\n", 0o644)
    assert placeholder_leaks(leaky, registry_secret_names()) == [
        f"{CONFIG} names ${{r2_access_key_id}}"
    ]


def test_the_unit_drop_in_replaces_the_installers_command_line(package):
    """The empty `ExecStart=` must come first, or systemd refuses a second ExecStart."""
    exec_lines = [
        line.partition("=")[2].strip()
        for line in text(package, DROPIN).splitlines()
        if line.startswith("ExecStart=")
    ]
    assert exec_lines == ["", "/usr/local/bin/k3s server"]


def test_the_package_marks_exactly_the_startup_files_for_the_k3s_restart(package):
    marked = {path for path, action in package.activations.items() if action == ACTION}
    assert marked == STARTUP_FILES
    assert package.policies[UNIT] == "windowed"
    for quiet in ("/etc/logrotate.d/k3s", MARKER):
        assert quiet in package.files and quiet not in package.activations


def test_the_logging_drop_in_sends_both_streams_to_the_log_file(package):
    content = text(package, DROPIN)
    log_file = component_defaults()["k3s_log_file"]
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
    assert spec["legacy"][0] == legacy


def test_the_coredns_marker_ships_empty(package):
    assert text(package, MARKER) == ""


def test_the_audit_log_directory_and_log_file_are_created_at_boot(package):
    tmpfiles = text(package, "/usr/lib/tmpfiles.d/homelab-k3s-server.conf")
    assert "f /var/log/k3s.log 0640 root adm -" in tmpfiles
    assert "d /var/lib/rancher/k3s/server/logs 0700 root root -" in tmpfiles


@pytest.mark.parametrize(
    ("host", "template"),
    [
        ("daniel-box", "k3s-server/templates/50-homelab.conf.j2"),
        ("daniel-server", "k3s-agent/templates/50-homelab.conf.j2"),
    ],
)
def test_a_template_naming_an_undefined_key_fails_the_host_render(
    tmp_path, host: str, template: str
):
    hosts = tmp_path / "hosts"
    shutil.copytree(HOSTS, hosts)
    # Sources outside hosts/ resolve against the root's parent.
    for name in ("k8s", "inventory", "secrets", "scripts", "docs"):
        (tmp_path / name).symlink_to(REPO / name)
    host_mode.render_host(
        host, hosts_root=hosts
    )  # the accept half: the copy renders as it stands
    broken = hosts / "components" / template
    broken.write_text("[Service]\nEnvironment=X={{ k3s_no_such_key }}\n")
    with pytest.raises(RenderError, match="k3s_no_such_key"):
        host_mode.render_host(host, hosts_root=hosts)
