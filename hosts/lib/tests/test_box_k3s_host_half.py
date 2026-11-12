"""daniel-box's package holds the k3s server's host half (H7.35).

`setup/k3s` wrote this state until H7.35. These tests hold what the move must keep: each cron
retires as its timer takes over on the same schedule, the file an installed unit reads exists at
the path it names, the live ufw rules are adopted rather than added, and a change to a netplan
file never reconfigures the link an SSH session rides on.

Run: uv run pytest hosts/lib/tests/test_box_k3s_host_half.py
"""

import dataclasses
import json
import os
import shutil
import types

import pytest

from lib import yaml_fast
from lib.estate import REPO_INVENTORY, role_defaults
from lib.proc_testing import run, write_exec
from lib.render_context import render_context
from lib.repo_paths import K3S_DEFAULTS, K8S_ROLES, REPO
from renderer.renderer_lib import host as host_mode

HOST = "daniel-box"
SYSTEM = "/usr/lib/systemd/system/"
COMPONENTS = REPO / "hosts" / "components"
TOOLS = REPO / "hosts" / "packages" / "homelab-tools" / "package.yml"
WIFI_FILE = "/etc/netplan/80-homelab-wifi-fallback.yaml"
WIFI_RENDERED = "/var/lib/homelab-secrets/netplan/80-homelab-wifi-fallback.yaml"
# Each cron file `setup/k3s` installed, with the timer that replaces it and its OnCalendar.
TIMERS = {
    "longhorn-backup-health": ("homelab-longhorn-backup-health", "*:00/10"),
    "disk-health": ("homelab-disk-health", "*:00/10"),
    "etcd-token-drift": ("homelab-etcd-token-drift", "*-*-* 2:45:00"),
    "b2-deletion-accounting": ("homelab-b2-deletion-accounting", "*-*-* 7:10:00"),
    "b2-budget-listing": ("homelab-b2-budget-listing", "*-*-* 7:20:00"),
    "etcd-restore-drill": ("homelab-etcd-restore-drill", "Mon *-*-* 10:20:00"),
}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host(HOST)


@pytest.fixture(scope="module")
def manifest(package) -> dict:
    return json.loads(package.files["/usr/lib/homelab/manifest.json"][0])


def text(package, path: str) -> str:
    body = package.files[path][0]
    return body.decode() if isinstance(body, bytes) else body


def _retired_paths(package) -> set[str]:
    return {e["path"] for e in package.legacy if "path" in e}


def test_the_media_directories_are_tmpfiles_lines_with_no_mode(package):
    lines = text(package, "/usr/lib/tmpfiles.d/homelab-media-volume.conf").splitlines()
    assert [line for line in lines if not line.startswith("#")] == [
        "d /srv/media - 1000 1000 -",
        "d /srv/media/media - 1000 1000 -",
        "d /srv/media/torrents - 1000 1000 -",
    ]


def test_the_media_path_copy_equals_the_k8s_roles_default():
    spec = yaml_fast.safe_load((REPO / "hosts" / HOST / "package.yml").read_text())
    assert (
        spec["defaults"]["media_volume_host_path"]
        == role_defaults(K8S_ROLES / "media-volume")["media_volume_host_path"]
    )


@pytest.mark.parametrize("cron", sorted(TIMERS))
def test_each_cron_retires_and_its_timer_keeps_the_schedule(package, manifest, cron):
    unit, calendar = TIMERS[cron]
    assert {"cron_file": cron} in package.legacy
    assert f"{unit}.timer" in manifest["enable_links"]
    assert f"OnCalendar={calendar}\n" in text(package, f"{SYSTEM}{unit}.timer")
    assert "ExecStart=" in text(package, f"{SYSTEM}{unit}.service")


@pytest.mark.parametrize(
    ("unit", "identifier"),
    [
        ("homelab-longhorn-backup-health", "longhorn-backup-health"),
        ("homelab-disk-health", "disk-health"),
        ("homelab-etcd-token-drift", "etcd-snapshot-offbox"),
        ("homelab-b2-deletion-accounting", "b2-deletions"),
        ("homelab-b2-budget-listing", "b2-budget"),
    ],
)
def test_a_service_logs_under_the_tag_its_readers_match(package, unit, identifier):
    service = text(package, f"{SYSTEM}{unit}.service")
    assert f"SyslogIdentifier={identifier}\n" in service
    # journald stores notice and above here, so an info line would never reach a reader.
    assert "SyslogLevel=notice\n" in service


@pytest.mark.parametrize(
    "unit",
    [
        "homelab-longhorn-backup-health",
        "homelab-disk-health",
        "homelab-etcd-token-drift",
    ],
)
def test_a_check_imports_the_ping_key_and_the_file_it_read_retires(package, unit):
    assert "ImportCredential=healthchecks_ping_key\n" in text(
        package, f"{SYSTEM}{unit}.service"
    )
    assert {"/etc/healthchecks/ping.env", "/etc/healthchecks"} <= _retired_paths(
        package
    )


def test_the_accounting_touches_the_stamp_check_10_reads(package):
    service = text(package, f"{SYSTEM}homelab-b2-deletion-accounting.service")
    shim = text(package, "/usr/lib/homelab/longhorn-backup-health.sh")
    assert (
        "ExecStartPre=-/usr/bin/touch /var/lib/longhorn-cron-fired/b2-deletion-accounting\n"
        in service
    )
    assert (
        'CRON_FIRED_STAMP="/var/lib/longhorn-cron-fired/b2-deletion-accounting"' in shim
    )


def test_check_10_stats_the_timer_the_package_ships(package):
    shim = text(package, "/usr/lib/homelab/longhorn-backup-health.sh")
    timer = f"{SYSTEM}homelab-b2-deletion-accounting.timer"
    assert f'LONGHORN_B2_DELETIONS_CRON_FILE="{timer}"' in shim
    assert timer in package.files


def test_the_restore_credentials_are_a_root_only_output_at_the_runbooks_path(package):
    (output,) = [o for o in package.outputs if "r2_bucket" in o["keys"]]
    assert output["destination"] == "etc/rancher/k3s/etcd-s3.env"
    assert (output["mode"], output["owner"]) == ("0600", "root")
    assert sorted(output["keys"]) == [
        "r2_access_key_id",
        "r2_account_id",
        "r2_bucket",
        "r2_secret_access_key",
    ]
    assert "/etc/rancher/k3s/etcd-s3.env" in package.links


def test_longhorns_prerequisites_and_multipath_drop_in(package, manifest):
    assert {"open-iscsi", "nfs-common", "multipath-tools"} <= set(package.depends)
    drop_in = "/etc/multipath/conf.d/50-homelab-longhorn.conf"
    assert 'devnode "^sd[a-z0-9]+"' in text(package, drop_in)
    assert package.activations[drop_in] == "reload:multipathd.service"
    assert "/etc/rancher/k3s/longhorn-backuptarget-r2.yaml" in _retired_paths(package)


def test_the_join_rules_equal_the_role_ports_and_are_kept(manifest):
    agent = render_context(None, HOST, inventory=REPO_INVENTORY)["k3s_agent_node_ips"][
        0
    ]
    expected = {
        f"allow from {agent} to any port {p['port']} proto {p['proto']}"
        for p in role_defaults(K3S_DEFAULTS.parents[1])["k3s_join_server_ports"]
    }
    rules = [r for r in manifest["ufw"]["rules"] if r["id"].startswith("k3s-join-")]
    assert {r["spec"] for r in rules} == expected
    assert all(r["keep"] for r in rules)


def test_each_live_k3s_rule_is_adopted_by_its_legacy_entry(manifest):
    rules = {
        r["spec"]: r["id"]
        for r in manifest["ufw"]["rules"]
        if r["id"].startswith(("k3s-join-", "coredns-host-"))
    }
    assert len(rules) == 8
    legacy = {e["spec"]: e for e in manifest["ufw"]["legacy"]}
    for spec, rule_id in rules.items():
        assert legacy[spec]["comment"] == ""
        assert legacy[spec]["replaced_by"] == rule_id


def test_the_forwarder_runs_the_pinned_binary_and_the_old_tree_retires(
    package, manifest
):
    unit = text(package, f"{SYSTEM}coredns-host.service")
    assert (
        "ExecStart=/usr/lib/homelab/bin/coredns -conf /etc/coredns/Corefile\n" in unit
    )
    assert "coredns-host.service" in manifest["enable_links"]
    assert {
        "/etc/systemd/system/coredns-host.service",
        "/usr/local/lib/coredns/1.14.7/coredns",
    } <= _retired_paths(package)
    pins = yaml_fast.safe_load(TOOLS.read_text())["tools"]["coredns"]
    assert pins["amd64"]["version"] == pins["arm64"]["version"]
    assert pins["amd64"]["member"] == "coredns"


def test_the_resolver_names_only_the_forwarder(package):
    lines = text(package, "/etc/resolv.conf").splitlines()
    assert [line for line in lines if line.startswith("nameserver")] == [
        "nameserver 127.0.0.1"
    ]


@pytest.mark.parametrize(
    "path",
    ["/etc/netplan/90-homelab-static-address.yaml", "/etc/netplan/99-homelab-dns.yaml"],
)
def test_a_netplan_file_goes_live_at_the_next_boot(package, path):
    assert package.activations[path] == "manual:systemd-networkd.service"
    assert package.files[path][1] == 0o600


def test_the_inotify_key_and_the_zshenv_block_use_the_line_arm(package):
    (inotify,) = [e for e in package.lines if "inotify" in e.get("line", "")]
    assert inotify["path"] == "/etc/sysctl.conf"
    assert inotify["line"] == "fs.inotify.max_user_instances=1024"
    (block,) = [e for e in package.lines if e["path"] == "/etc/zsh/zshenv"]
    # The Ansible task's marker, so the first run adopts its block and adds no second copy.
    assert block["marker"] == "# {mark} ANSIBLE MANAGED — k3s kubeconfig"
    assert ". /etc/profile.d/k3s-kubeconfig.sh" in block["block"]
    # The arm creates a missing /etc/zsh 0700, so zsh has to install the file first.
    assert "zsh" in package.depends


def _run_kubeconfig(package, tmp_path, admin: str | None):
    owner = os.environ.get("USER") or "ubuntu"
    script = write_exec(
        tmp_path / "k3s-readonly-kubeconfig",
        text(package, "/usr/lib/homelab/k3s-readonly-kubeconfig"),
    )
    if admin is not None:
        path = tmp_path / "etc/rancher/k3s/k3s.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(admin)
    creds = tmp_path / "creds"
    creds.mkdir(exist_ok=True)
    (creds / "k3s_readonly_token").write_text("the-token")
    return run(
        [str(script)],
        env={
            **os.environ,
            "HOMELAB_ROOT": str(tmp_path),
            "KUBECONFIG_OWNER": owner,
            "CREDENTIALS_DIRECTORY": str(creds),
        },
        check=False,
    )


def test_the_kubeconfig_oneshot_writes_the_token_and_the_admin_ca(package, tmp_path):
    result = _run_kubeconfig(
        package,
        tmp_path,
        "clusters:\n- cluster:\n    certificate-authority-data: Q0E=\n",
    )
    assert result.returncode == 0, result.stderr
    dest = next(tmp_path.glob("home/*/.kube/config"))
    config = yaml_fast.safe_load(dest.read_text())
    assert config["users"][0]["user"]["token"] == "the-token"
    assert config["clusters"][0]["cluster"] == {
        "server": "https://127.0.0.1:6443",
        "certificate-authority-data": "Q0E=",
    }
    assert oct(dest.stat().st_mode & 0o777) == "0o600"


def test_the_kubeconfig_oneshot_refuses_an_admin_file_with_no_ca(package, tmp_path):
    result = _run_kubeconfig(package, tmp_path, "clusters: []\n")
    assert result.returncode == 1
    assert "no certificate-authority-data" in result.stderr
    assert not list(tmp_path.glob("home/*/.kube/config"))


def test_a_rotated_token_restarts_the_kubeconfig_oneshot(package):
    (output,) = [o for o in package.outputs if o["keys"] == ["k3s_readonly_token"]]
    assert output["dependents"] == ["homelab-k3s-kubeconfig.service"]
    unit = text(package, f"{SYSTEM}homelab-k3s-kubeconfig.service")
    assert (
        f"LoadCredential=k3s_readonly_token:/run/homelab-secrets/{output['destination']}\n"
        in unit
    )


def test_the_kubeconfig_oneshot_waits_for_k3s_rather_than_failing(package):
    # A rebuild installs the package before k3s starts, and a failed unit would hold the host.
    unit = text(package, f"{SYSTEM}homelab-k3s-kubeconfig.service")
    assert "ConditionPathExists=/etc/rancher/k3s/k3s.yaml\n" in unit


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


def test_the_wifi_secrets_reach_netplan_only_through_the_guarded_copy(package):
    """A symlink in /etc/netplan would fail `netplan generate` while the output is missing."""
    assert WIFI_FILE not in package.links and WIFI_FILE not in package.files
    (output,) = [o for o in package.outputs if "wifi_password" in o["keys"]]
    assert output["persist"] is True
    assert output["dependents"] == ["homelab-netplan-wifi.service"]


@pytest.mark.parametrize("password", ['pa"ss', "ab\\tc", "plain"])
def test_a_password_reaches_netplan_unchanged(package, password):
    file = _wifi_file(package, 'home "net"', password)
    assert file["network"]["wifis"]["wlp4s0"]["access-points"] == {
        'home "net"': {"password": password}
    }


def _run_wifi_copy(tmp_path, generate_rc: int):
    rendered = tmp_path / WIFI_RENDERED.lstrip("/")
    rendered.parent.mkdir(parents=True)
    rendered.write_text("rendered\n")
    (tmp_path / "etc/netplan").mkdir(parents=True)
    (tmp_path / WIFI_FILE.lstrip("/")).write_text("old\n")
    generate = tmp_path / "generate"
    write_exec(generate, f"#!/bin/sh\nexit {generate_rc}\n")
    return run(
        [str(COMPONENTS / "k3s-server" / "files" / "homelab-netplan-wifi")],
        env={
            **os.environ,
            "HOMELAB_ROOT": str(tmp_path),
            "NETPLAN_GENERATE": str(generate),
        },
        check=False,
    )


def test_the_wifi_copy_installs_a_file_netplan_accepts(tmp_path):
    assert _run_wifi_copy(tmp_path, 0).returncode == 0
    assert (tmp_path / WIFI_FILE.lstrip("/")).read_text() == "rendered\n"


def test_the_wifi_copy_leaves_the_old_file_when_netplan_refuses(tmp_path):
    assert _run_wifi_copy(tmp_path, 1).returncode == 1
    assert (tmp_path / WIFI_FILE.lstrip("/")).read_text() == "old\n"


def test_emptying_the_wifi_link_drops_the_units_and_retires_both_copies(tmp_path):
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO_INVENTORY.host_vars, host_vars)
    box = host_vars / f"{HOST}.yml"
    named = "k3s_node_wifi_link: wlp4s0"
    assert named in box.read_text()
    box.write_text(box.read_text().replace(named, 'k3s_node_wifi_link: ""'))
    emptied = host_mode.render_host(
        HOST, inventory=dataclasses.replace(REPO_INVENTORY, host_vars=host_vars)
    )
    assert not [o for o in emptied.outputs if "wifi_password" in o["keys"]]
    assert f"{SYSTEM}homelab-netplan-wifi.service" not in emptied.files
    assert {WIFI_FILE, WIFI_RENDERED} <= _retired_paths(emptied)
