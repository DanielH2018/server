"""The `k3s-agent` component as daniel-server's package renders it (H6.5a, H6.5b).

The package ships the files Ansible's `tasks/agent.yml` wrote, so dpkg takes each path over,
and since H6.5b the agent's flags in config.yaml.d. It
marks each file k3s reads at startup `manual:k3s-agent.service`, and the package marks the unit
`windowed`, so reconcile never restarts the agent and the change goes live at its next start.
Every failure here is silent until that start: a drop-in that leaves stdout in the journal loses
a crash loop's reason, and a file with no activation is never reported pending.
"""

import dataclasses
import os
import shutil
import types

import pytest
from _helpers import load_tasks, load_yaml
from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.proc_testing import run, write_exec
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE, REPO
from renderer.renderer_lib import host as host_mode

COMPONENT = REPO / "hosts" / "components" / "k3s-agent"
WIFI_FILE = "/etc/netplan/80-homelab-wifi-fallback.yaml"
WIFI_RENDERED = "/var/lib/homelab-secrets/netplan/80-homelab-wifi-fallback.yaml"
UNIT = "k3s-agent.service"
DROPIN = "/usr/lib/systemd/system/k3s-agent.service.d/50-homelab.conf"
CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"
STARTUP_FILES = {DROPIN, CONFIG, "/etc/rancher/k3s/registries.yaml"}
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


def test_the_join_rules_are_kept_and_match_the_ports_the_server_side_mirrors(package):
    server = "10.0.0.215"
    want = {
        f"allow from {server} to any port {p['port']} proto {p['proto']}"
        for p in load_yaml(K3S_DEFAULTS)["k3s_join_agent_ports"]
    }
    join = [r for r in package.ufw_rules if r["id"].startswith("k3s-join-")]
    assert {r["spec"] for r in join} == want
    assert all(r["keep"] for r in join)
    assert {r["spec"] for r in package.ufw_legacy} >= want


def test_the_longhorn_packages_and_the_inotify_key_ship_in_the_package(package):
    assert {"open-iscsi", "nfs-common", "multipath-tools"} <= set(package.depends)
    assert {
        "path": "/etc/sysctl.conf",
        "line": "fs.inotify.max_user_instances=1024",
        "regexp": r"^\s*fs\.inotify\.max_user_instances\s*=",
    }.items() <= next(
        edit for edit in package.lines if edit.get("line", "").startswith("fs.inotify")
    ).items()
    assert (
        package.activations["/etc/multipath/conf.d/50-homelab-longhorn.conf"]
        == "reload:multipathd.service"
    )


def test_the_pin_ships_with_its_linkdown_keys_and_never_applies_itself(package):
    assert (
        package.activations["/etc/netplan/90-homelab-static-address.yaml"]
        == "manual:systemd-networkd.service"
    )
    assert "enp88s0:" in text(package, "/etc/netplan/90-homelab-static-address.yaml")
    keys = text(package, "/etc/sysctl.d/90-homelab-static-link.conf")
    for family in ("ipv4", "ipv6"):
        assert f"net.{family}.conf.enp88s0.ignore_routes_with_linkdown=1" in keys


def test_the_wifi_secrets_reach_netplan_only_through_the_guarded_copy(package):
    """A symlink in /etc/netplan would fail `netplan generate` while the output is missing."""
    fallback = "/etc/netplan/80-homelab-wifi-fallback.yaml"
    assert fallback not in package.links and fallback not in package.files
    (output,) = [o for o in package.outputs if "wifi_password" in o["keys"]]
    assert output["persist"] is True
    assert output["format"] == "json"
    assert output["dependents"] == ["homelab-netplan-wifi.service"]
    unit = text(package, "/usr/lib/systemd/system/homelab-netplan-wifi.service")
    assert (
        f"ConditionPathExists=/var/lib/homelab-secrets/{output['destination']}" in unit
    )


def _run_wifi_copy(tmp_path, generate_rc: int):
    rendered = (
        tmp_path / "var/lib/homelab-secrets/netplan/80-homelab-wifi-fallback.yaml"
    )
    rendered.parent.mkdir(parents=True)
    rendered.write_text("rendered\n")
    (tmp_path / "etc/netplan").mkdir(parents=True)
    (tmp_path / "etc/netplan/80-homelab-wifi-fallback.yaml").write_text("old\n")
    generate = tmp_path / "generate"
    write_exec(generate, f"#!/bin/sh\nexit {generate_rc}\n")
    return run(
        [str(COMPONENT / "files" / "homelab-netplan-wifi")],
        env={
            **os.environ,
            "HOMELAB_ROOT": str(tmp_path),
            "NETPLAN_GENERATE": str(generate),
        },
    )


def test_a_rendered_file_netplan_accepts_replaces_the_old_one(tmp_path):
    assert _run_wifi_copy(tmp_path, 0).returncode == 0
    installed = tmp_path / "etc/netplan/80-homelab-wifi-fallback.yaml"
    assert installed.read_text() == "rendered\n"
    assert installed.stat().st_mode & 0o777 == 0o600


def test_a_rendered_file_netplan_refuses_leaves_the_old_one(tmp_path):
    result = _run_wifi_copy(tmp_path, 1)
    assert result.returncode == 1
    assert "netplan refuses" in result.stderr
    assert (
        tmp_path / "etc/netplan/80-homelab-wifi-fallback.yaml"
    ).read_text() == "old\n"


# Each node's package ships its own copy of the wifi installer; a fix to one must reach both.
WIFI_COPIES = (
    "files/homelab-netplan-wifi",
    "units/homelab-netplan-wifi.service",
    "templates/netplan-wifi-fallback.json.j2",
)


def wifi_copies_that_differ(server, agent) -> list[str]:
    """The `WIFI_COPIES` whose bytes differ between the two component directories."""
    return [
        name
        for name in WIFI_COPIES
        if (server / name).read_bytes() != (agent / name).read_bytes()
    ]


def test_the_server_and_agent_wifi_copies_are_equal():
    server = REPO / "hosts" / "components" / "k3s-server"
    assert wifi_copies_that_differ(server, COMPONENT) == []


def test_a_wifi_copy_edited_on_one_node_is_flagged(tmp_path):
    for side in ("server", "agent"):
        for name in WIFI_COPIES:
            (tmp_path / side / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / side / name).write_text("same\n")
    (tmp_path / "agent" / WIFI_COPIES[1]).write_text("edited\n")
    assert wifi_copies_that_differ(tmp_path / "server", tmp_path / "agent") == [
        WIFI_COPIES[1]
    ]


def test_the_join_task_file_refuses_and_names_the_bootstrap():
    (task,) = load_tasks(K3S_ROLE / "tasks" / "agent.yml")
    assert "bootstrap-host.sh" in task["ansible.builtin.fail"]["msg"]


def _wifi_file(package, ssid: str, password: str) -> dict:
    """The installed wifi file as netplan parses it, through homelab-secrets' own substitution."""
    script = REPO / "hosts" / "lib" / "homelab-secrets"
    secrets = types.ModuleType("homelab_secrets")
    exec(compile(script.read_text(), str(script), "exec"), secrets.__dict__)
    (output,) = [o for o in package.outputs if "wifi_password" in o["keys"]]
    rendered = secrets.substitute(
        text(package, f"/usr/lib/homelab/secret-templates{WIFI_FILE}"),
        set(output["keys"]),
        {"wifi_ssid": ssid, "wifi_password": password},
        output.get("format"),
    )
    return yaml_fast.safe_load(rendered)


@pytest.mark.parametrize("password", ['pa"ss', "ab\\tc", "plain"])
def test_a_password_reaches_netplan_unchanged(package, password):
    file = _wifi_file(package, 'home "net"', password)
    assert file["network"]["wifis"]["wlo1"]["access-points"] == {
        'home "net"': {"password": password}
    }


def test_emptying_the_wifi_link_drops_the_units_and_retires_both_copies(tmp_path):
    """netplan refuses an empty `wifis:`, so the output must go rather than render empty."""
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO_INVENTORY.host_vars, host_vars)
    server = host_vars / "daniel-server.yml"
    named = "k3s_node_wifi_link: wlo1"
    assert named in server.read_text()
    server.write_text(server.read_text().replace(named, 'k3s_node_wifi_link: ""'))
    package = host_mode.render_host(
        "daniel-server",
        inventory=dataclasses.replace(REPO_INVENTORY, host_vars=host_vars),
    )
    assert not [o for o in package.outputs if "wifi_password" in o["keys"]]
    assert "/usr/lib/homelab/homelab-netplan-wifi" not in package.files
    assert "/usr/lib/systemd/system/homelab-netplan-wifi.service" not in package.files
    assert {WIFI_FILE, WIFI_RENDERED} <= {e.get("path") for e in package.legacy}


def test_a_named_wifi_link_retires_neither_copy(package):
    assert not {WIFI_FILE, WIFI_RENDERED} & {e.get("path") for e in package.legacy}
