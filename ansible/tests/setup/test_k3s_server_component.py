"""The `k3s-server` component as daniel-box's package renders it (H6.4a).

The package ships the files Ansible's `tasks/server.yml` wrote, so dpkg takes each path over, and
marks each file k3s reads at startup `manual:k3s.service`. `homelab-k3s-restart` hashes exactly
those entries, so the next window restarts k3s once. Every failure here is silent until that
restart: a flag that names a file the package does not ship wedges the API server (the unit is
Type=notify with TimeoutStartSec=0), and a file with no activation never reaches the hash.

The package needs `k3s_readonly_token` in `secrets.yml` and a daniel-box SOPS file to render, so
the fixtures stand in for both, as `hosts/lib/tests/test_k3s_restart_component.py` does.
"""

import re
import shutil

import jinja2
import pytest
from _helpers import load_defaults
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import ALL_VARS, K3S_ROLE, REPO
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError

HOSTS = REPO / "hosts"
COMPONENT = HOSTS / "components" / "k3s-server"
UNIT = "k3s.service"
ACTION = f"manual:{UNIT}"
DROPIN = "/usr/lib/systemd/system/k3s.service.d/50-homelab.conf"
# The flags whose value is a file the apiserver reads at startup and refuses to start without.
FILE_FLAGS = ("audit-policy-file", "authentication-config")
# What the restart unit hashes: every file the package ships that k3s reads at startup.
STARTUP_FILES = {
    DROPIN,
    "/var/lib/rancher/k3s/server/audit.yaml",
    "/var/lib/rancher/k3s/server/authentication-config.yaml",
    "/etc/rancher/k3s/registries.yaml",
}
MARKER = "/var/lib/rancher/k3s/server/manifests/coredns.yaml.skip"


def _stage(root, hosts_root=HOSTS):
    (root / "secrets.yml").write_text("k3s_readonly_token: x\n")
    (root / "sops").mkdir()
    (root / "sops/daniel-box.sops.yaml").write_text("k3s_readonly_token: x\n")
    return host_mode.render_host(
        "daniel-box",
        hosts_root=hosts_root,
        secrets=root / "secrets.yml",
        sops_dir=root / "sops",
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return _stage(tmp_path_factory.mktemp("box"))


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


def rendered_server_args() -> str:
    """`k3s_server_args` as the installer receives it, with the audit flags armed."""
    defaults = load_defaults(K3S_ROLE)
    context = {
        "server_ip": "10.0.0.215",
        **defaults,
        **yaml_fast.safe_load(ALL_VARS.read_text()),
        "k3s_audit_log_enabled": True,
    }
    env = make_ansible_env(undefined_cls=jinja2.StrictUndefined)
    for key in ("k3s_audit_log_path", "k3s_audit_log_dir"):
        value = str(context[key])
        if "{{" in value:
            context[key] = env.from_string(value).render(**context)
    return env.from_string(defaults["k3s_server_args"]).render(**context)


def file_flag_values(args: str) -> dict[str, str]:
    return dict(re.findall(r"--kube-apiserver-arg=([a-z-]+)=(\S+)", args))


def test_every_file_a_k3s_flag_names_is_a_file_the_package_ships(package):
    values = file_flag_values(rendered_server_args())
    assert set(FILE_FLAGS) <= set(values), "the flags this test reads are gone"
    for flag in FILE_FLAGS:
        assert values[flag] in package.files, (
            f"--{flag}={values[flag]} names a file daniel-box's package does not ship, so "
            "the next k3s start finds no such file and the API server never comes up"
        )


COPIED_KEYS = (
    "k3s_audit_policy_path",
    "k3s_authentication_config_path",
    "k3s_log_file",
)


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
    assert package.legacy == [
        {"path": "/etc/systemd/system/k3s.service.d/10-log-to-file.conf"}
    ]


def test_the_coredns_marker_ships_empty(package):
    assert text(package, MARKER) == ""


def test_the_audit_log_directory_and_log_file_are_created_at_boot(package):
    tmpfiles = text(package, "/usr/lib/tmpfiles.d/homelab-k3s-server.conf")
    assert "f /var/log/k3s.log 0640 root adm -" in tmpfiles
    assert "d /var/lib/rancher/k3s/server/logs 0700 root root -" in tmpfiles


def test_a_template_naming_an_undefined_key_fails_the_host_render(tmp_path):
    hosts = tmp_path / "hosts"
    shutil.copytree(HOSTS, hosts)
    (tmp_path / "ok").mkdir()
    _stage(tmp_path / "ok", hosts)  # the accept half: the copy renders as it stands
    broken = hosts / "components/k3s-server/templates/50-homelab.conf.j2"
    broken.write_text(broken.read_text() + "Environment=X={{ k3s_no_such_key }}\n")
    (tmp_path / "bad").mkdir()
    with pytest.raises(RenderError, match="k3s_no_such_key"):
        _stage(tmp_path / "bad", hosts)
