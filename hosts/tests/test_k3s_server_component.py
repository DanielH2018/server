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
from lib.repo_paths import HOST_VARS, K3S_DEFAULTS, K3S_SERVER, K8S_ROLES, REPO
from lib.longhorn_lib import longhorn_etcd_s3_logic as etcd_s3
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.environment import RenderError

HOSTS = REPO / "hosts"
COMPONENT = K3S_SERVER
UNIT = "k3s.service"
ACTION = f"manual:{UNIT}"
DROPIN = "/usr/lib/systemd/system/k3s.service.d/50-homelab.conf"
CONFIG_DIR = "/etc/rancher/k3s/config.yaml.d/"
CONFIG = CONFIG_DIR + "50-homelab.yaml"
GRACEFUL_SHUTDOWN = (
    "/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/50-homelab-graceful-shutdown.conf"
)
# The flags whose value is a file the apiserver reads at startup and refuses to start without.
FILE_FLAGS = ("audit-policy-file", "authentication-config")
# What the restart unit hashes: every file the package ships that k3s reads at startup.
STARTUP_FILES = {
    DROPIN,
    CONFIG,
    "/var/lib/rancher/k3s/server/audit.yaml",
    "/var/lib/rancher/k3s/server/authentication-config.yaml",
    "/etc/rancher/k3s/registries.yaml",
    GRACEFUL_SHUTDOWN,
}
MARKER = "/var/lib/rancher/k3s/server/manifests/coredns.yaml.skip"
CPU_WEIGHT = "/usr/lib/systemd/system/k3s.service.d/60-homelab-cpu-weight.conf"
SYSTEM_SLICE_WEIGHT = (
    "/usr/lib/systemd/system/system.slice.d/60-homelab-cpu-weight.conf"
)


@pytest.fixture(scope="module")
def package():
    return host_mode.render_host("daniel-box")


@pytest.fixture(scope="module", params=["daniel-box", "daniel-server"])
def node_package(request):
    """Each k3s node's package: k3s-agent ships this component's graceful-shutdown files too."""
    return host_mode.render_host(request.param)


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


def shutdown_groups(package: host_mode.Package) -> dict[int, int]:
    config = yaml_fast.safe_load(text(package, GRACEFUL_SHUTDOWN))
    assert config["kind"] == "KubeletConfiguration"
    assert "shutdownGracePeriod" not in config
    return {
        group["priority"]: group["shutdownGracePeriodSeconds"]
        for group in config["shutdownGracePeriodByPodPriority"]
    }


def test_shutdown_stops_workloads_before_longhorn_and_longhorn_before_system_pods(
    node_package,
):
    """Longhorn's group opens at its own PriorityClass, so a bump that moves it fails here."""
    upstream = K8S_ROLES / "longhorn" / "files" / "upstream" / "longhorn.yaml"
    longhorn = next(
        doc["value"]
        for doc in yaml_fast.safe_load_all(upstream.read_text())
        if doc
        and doc["kind"] == "PriorityClass"
        and doc["metadata"]["name"] == "longhorn-critical"
    )
    assert sorted(shutdown_groups(node_package)) == [0, longhorn, 2000000000]


def test_the_whole_shutdown_fits_inside_the_ups_low_battery_margin(node_package):
    """An FSD raised at battery.runtime.low leaves 120 s, and poweroff waits on kubelet's lock."""
    assert sum(shutdown_groups(node_package).values()) < 120


def test_logind_lets_kubelet_delay_the_shutdown_for_every_group(node_package):
    """kubelet's shutdown manager refuses to start while logind's delay is below the groups' sum.

    logind merges drop-ins in filename order across /etc and /usr/lib, and the last one wins, so
    the file must sort after kubelet's own and after unattended-upgrades', which sets 30 s.
    """
    (path,) = [p for p in node_package.files if "/logind.conf.d/" in p]
    name = path.rsplit("/", 1)[1]
    assert name > "unattended-upgrades-logind-maxdelay.conf"
    assert name > "99-kubelet.conf"
    assert node_package.activations[path] == "reload:systemd-logind.service"
    delay = setting(text(node_package, path), "InhibitDelayMaxSec")
    assert delay == str(sum(shutdown_groups(node_package).values()))


def test_the_cpu_weight_drop_in_ranks_k3s_above_its_siblings_with_no_restart(package):
    """systemd's default weight is 100, so a weight at or below it ranks k3s no higher."""
    weight = yaml_fast.safe_load(K3S_DEFAULTS.read_text())["k3s_cpu_weight"]
    assert setting(text(package, CPU_WEIGHT), "CPUWeight") == str(weight)
    assert int(weight) > 100
    assert CPU_WEIGHT not in package.activations


def test_the_system_slice_drop_in_outweighs_kubepods_with_no_restart(package):
    """kubelet weights kubepods.slice at 625 on daniel-box's 16 cores, the larger node."""
    weight = yaml_fast.safe_load(K3S_DEFAULTS.read_text())[
        "k3s_system_slice_cpu_weight"
    ]
    content = text(package, SYSTEM_SLICE_WEIGHT)
    assert "[Slice]" in content.splitlines()
    assert setting(content, "CPUWeight") == str(weight)
    assert int(weight) > 625
    assert SYSTEM_SLICE_WEIGHT not in package.activations


UNIT_DIR = "/usr/lib/systemd/system/"
KUBEPODS_DROPINS = UNIT_DIR + "kubepods.slice.d/"
KUBELET_MEMORY_LOW = "/run/systemd/transient/kubepods.slice.d/50-MemoryLow.conf"
REAPPLY = UNIT_DIR + "homelab-kubepods-memory-low"
K3S_UNIT = {
    "homelab-daniel-box": "k3s.service",
    "homelab-daniel-server": "k3s-agent.service",
}


def mebibytes(size: str) -> int:
    return int(size[:-1]) * {"M": 1, "G": 1024}[size[-1]]


def test_each_node_renders_its_own_memory_low_and_k3s_fits_inside_system_slice(
    node_package,
):
    """A child's effective memory.low never exceeds its parent's, so k3s fits system.slice."""
    host = node_package.name.removeprefix("homelab-")
    values = {
        **yaml_fast.safe_load(K3S_DEFAULTS.read_text()),
        **yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()),
    }
    dropin = "60-homelab-memory-low.conf"
    rendered = {
        "k3s_kubepods_memory_low": KUBEPODS_DROPINS + dropin,
        "k3s_system_slice_memory_low": f"{UNIT_DIR}system.slice.d/{dropin}",
        "k3s_memory_low": f"{UNIT_DIR}{K3S_UNIT[node_package.name]}.d/{dropin}",
    }
    for key, path in rendered.items():
        content = text(node_package, path)
        assert setting(content, "MemoryLow") == values[key]
        assert setting(content, "MemoryMin") is None
        assert path not in node_package.activations
    assert mebibytes(values["k3s_memory_low"]) <= mebibytes(
        values["k3s_system_slice_memory_low"]
    )


def test_kubelets_reset_of_kubepods_memory_low_is_overridden_and_reapplied(
    node_package,
):
    """kubelet writes MemoryLow=0 to the transient drop-in at each start; a later name wins."""
    # fact: hosts/components/k3s-server/CLAUDE.md#Autonomous-role contract (`homelab-kubepods-memory-low.path`, the reload after kubelet's reset)
    kubelet_name = KUBELET_MEMORY_LOW.rsplit("/", 1)[1]
    names = [
        path.removeprefix(KUBEPODS_DROPINS)
        for path in node_package.files
        if path.startswith(KUBEPODS_DROPINS)
    ]
    assert names
    assert all(name > kubelet_name for name in names), names
    assert setting(text(node_package, REAPPLY + ".path"), "PathChanged") == (
        KUBELET_MEMORY_LOW
    )
    service = text(node_package, REAPPLY + ".service")
    assert setting(service, "ExecStart") == "/usr/bin/systemctl daemon-reload"
    wants = UNIT_DIR + "paths.target.wants/homelab-kubepods-memory-low.path"
    assert node_package.links[wants] == "../homelab-kubepods-memory-low.path"


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
