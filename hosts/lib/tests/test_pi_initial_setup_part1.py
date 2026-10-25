"""The Pi's first three `initial_setup` subjects as packaged components (H7.27).

`unattended-upgrades`, `sshd` and `host-tuning` replace initial_setup's apt, sshd and sysctl
tasks on daniel-pi. These tests hold what the move added: the rendered files, the line-arm
edits that adopt what Ansible wrote, the `activate:` actions, and sshd's own reading of the two
drop-ins. Each component is host-neutral, so daniel-server and daniel-box reuse it.

Run: uv run pytest hosts/lib/tests/test_pi_initial_setup_part1.py
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _reconcile_arms import live_unit
from _reconcile_host import V1, Node

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from renderer.renderer_lib import host as host_mode

SSHD_D = "/etc/ssh/sshd_config.d"
SYSCTL = "/etc/sysctl.conf"
# The twenty keys initial_setup's two `ansible.posix.sysctl` tasks set on the Pi, and the two
# zram keys that joined them from optimize_pi in H7.25.
SYSCTL_KEYS = {
    "vm.swappiness": "130",
    "vm.page-cluster": "0",
    "net.ipv4.ip_forward": "1",
    "net.ipv4.conf.all.rp_filter": "2",
    "net.ipv4.conf.all.accept_redirects": "0",
    "net.ipv4.conf.default.accept_redirects": "0",
    "net.ipv4.conf.all.send_redirects": "0",
    "net.ipv4.conf.default.send_redirects": "0",
    "net.ipv4.conf.all.accept_source_route": "0",
    "net.ipv4.conf.default.accept_source_route": "0",
    "net.ipv6.conf.all.accept_source_route": "0",
    "net.ipv6.conf.default.accept_source_route": "0",
    "net.ipv4.icmp_echo_ignore_broadcasts": "1",
    "net.ipv4.conf.all.log_martians": "1",
    "net.ipv4.conf.default.log_martians": "1",
    "kernel.sysrq": "0",
    "kernel.dmesg_restrict": "1",
    "kernel.kptr_restrict": "2",
    "net.core.bpf_jit_harden": "2",
    "fs.suid_dumpable": "0",
    "fs.protected_fifos": "2",
    "dev.tty.ldisc_autoload": "0",
}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_host("daniel-pi")


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _edits(package: host_mode.Package, path: str) -> list[dict]:
    return [edit for edit in package.lines if edit["path"] == path]


def test_the_apt_files_carry_the_periodic_settings_and_every_origin_pattern(package):
    periodic = _text(package, "/etc/apt/apt.conf.d/20auto-upgrades")
    assert 'APT::Periodic::Unattended-Upgrade "1";' in periodic
    local = _text(package, "/etc/apt/apt.conf.d/52unattended-upgrades-local")
    patterns = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)[
        "unattended_upgrades_origins_patterns"
    ]
    assert patterns, "the Pi must keep its extra origins"
    # The `::` form appends, so a `{ ... }` block or Allowed-Origins here would drop -security.
    appended = re.findall(
        r'^Unattended-Upgrade::Origins-Pattern:: "(.*)";$', local, re.M
    )
    assert appended == patterns
    assert "Allowed-Origins" not in local
    assert 'Automatic-Reboot "false";' in local
    assert "\n\n" not in local


def test_the_sshd_settings_sit_in_the_first_drop_in_and_the_match_block_in_the_last(
    package,
):
    settings = dict(
        line.split(None, 1)
        for line in _text(package, f"{SSHD_D}/10-homelab.conf").splitlines()
        if line and not line.startswith("#")
    )
    assert settings["PasswordAuthentication"] == "no"
    assert settings["PermitRootLogin"] == "no"
    assert settings["MaxAuthTries"] == "3"
    assert settings["AllowGroups"] == "ssh-users"
    assert settings["AllowTcpForwarding"] == "no"
    assert len(settings) == 17
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]
    match = _text(package, f"{SSHD_D}/90-homelab-match.conf")
    assert f"Match User {user}\n" in match
    assert "    AllowTcpForwarding all\n" in match
    assert package.activations[f"{SSHD_D}/10-homelab.conf"] == "reload:ssh.service"
    assert (
        package.activations[f"{SSHD_D}/90-homelab-match.conf"] == "reload:ssh.service"
    )


@pytest.mark.skipif(not shutil.which("sshd"), reason="needs OpenSSH's sshd")
def test_sshd_accepts_both_drop_ins_and_widens_forwarding_only_for_the_sys_user(
    package, tmp_path
):
    sshd = Path(shutil.which("sshd") or "")
    drop_ins = tmp_path / "sshd_config.d"
    drop_ins.mkdir()
    for name in ("10-homelab.conf", "90-homelab-match.conf"):
        (drop_ins / name).write_bytes(package.files[f"{SSHD_D}/{name}"][0])
    # cloud-init's own file, last in the order, with the value the package must outrank.
    (drop_ins / "50-cloud-init.conf").write_text("PasswordAuthentication yes\n")
    key = tmp_path / "host_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    config = tmp_path / "sshd_config"
    config.write_text(f"Include {drop_ins}/*.conf\nHostKey {key}\n")
    user = render_context(None, "daniel-pi", inventory=REPO_INVENTORY)["sys_user"]

    def effective(who: str) -> dict[str, str]:
        done = subprocess.run(
            [
                str(sshd),
                "-T",
                "-f",
                str(config),
                "-C",
                f"user={who},host=x,addr=10.0.0.1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return dict(line.split(None, 1) for line in done.stdout.splitlines())

    mine, other = effective(user), effective("nobody")
    assert mine["passwordauthentication"] == other["passwordauthentication"] == "no"
    assert mine["allowtcpforwarding"] == "yes" and other["allowtcpforwarding"] == "no"
    assert {k for k in mine if mine[k] != other[k]} <= {
        "allowtcpforwarding",
        "allowstreamlocalforwarding",
        "disableforwarding",
        "streamlocalbindunlink",
    }


def test_the_sysctl_edits_cover_exactly_the_keys_ansible_set_and_each_adopts_its_line(
    package,
):
    edits = _edits(package, SYSCTL)
    assert {e["line"] for e in edits} == {f"{k}={v}" for k, v in SYSCTL_KEYS.items()}
    for edit in edits:
        # The first run adopts the `<key>=<value>` line ansible.posix.sysctl wrote.
        assert re.search(edit["regexp"], edit["line"])
        assert not re.search(edit["regexp"], "x" + edit["line"])
        assert package.activations[SYSCTL] == "sysctl"
    keys = [e["line"].split("=")[0] for e in edits]
    assert len(keys) == len(set(keys))


def test_a_sysctl_regexp_matches_only_its_own_key(package):
    for edit in _edits(package, SYSCTL):
        key = edit["line"].split("=")[0]
        others = [k for k in SYSCTL_KEYS if k != key]
        assert not any(re.search(edit["regexp"], f"{k}=0") for k in others), key


def test_ufw_and_login_defs_edits_keep_the_anchors_of_the_tasks_they_replace(package):
    ufw = {e["line"]: e["regexp"] for e in _edits(package, "/etc/ufw/sysctl.conf")}
    assert set(ufw) == {
        "net/ipv4/conf/all/log_martians=1",
        "net/ipv4/conf/default/log_martians=1",
    }
    # Ubuntu ships the stock line uncommented at 0, so the edit must replace that line.
    for line, regexp in ufw.items():
        assert re.search(regexp, line.replace("=1", "=0"))
    defs = {e["line"].split("\t")[0]: e for e in _edits(package, "/etc/login.defs")}
    assert set(defs) == {
        "UMASK",
        "SHA_CRYPT_MIN_ROUNDS",
        "SHA_CRYPT_MAX_ROUNDS",
        "PASS_MIN_DAYS",
        "PASS_MAX_DAYS",
        "PASS_WARN_AGE",
    }
    assert defs["UMASK"]["line"] == "UMASK\t\t027"
    assert re.search(
        defs["SHA_CRYPT_MIN_ROUNDS"]["regexp"], "#SHA_CRYPT_MIN_ROUNDS 5000"
    )


def test_journald_and_limits_ship_as_drop_ins(package):
    journald = _text(package, "/etc/systemd/journald.conf.d/50-homelab.conf")
    assert "SystemMaxUse=1G\nMaxLevelStore=notice\nMaxLevelSyslog=info\n" in journald
    assert (
        package.activations["/etc/systemd/journald.conf.d/50-homelab.conf"]
        == "try-restart:systemd-journald.service"
    )
    assert "* hard core 0\n" in _text(package, "/etc/security/limits.d/50-homelab.conf")


def test_the_pis_real_manifest_reconciles_clean_and_reloads_sysctl_once(
    package, tmp_path
):
    """The line arm edits conffiles a real host has, so the node seeds each with Ansible's lines."""
    node = Node(tmp_path)
    node.manifest = json.loads(package.files["/usr/lib/homelab/manifest.json"][0])
    # The package ships `secrets-unit` (H4.5): reconcile needs its secrets.json and credentials.
    secrets = package.files["/usr/lib/homelab/secrets.json"][0]
    (node.root / "usr/lib/homelab").mkdir(parents=True, exist_ok=True)
    (node.root / "usr/lib/homelab/secrets.json").write_bytes(secrets)
    for key in json.loads(secrets)["credentials"]:
        node.credential(key)
    for name in node.manifest["units"]:
        node.state["units"][name] = {
            "load": "loaded",
            "active": "inactive",
            "enabled": "disabled",
        }
    # The Pi runs each unit its edits activate, and its tmpfiles line made the textfile directory.
    for unit in (
        "ssh.service",
        "systemd-journald.service",
        "auditd.service",
        "apt-daily.timer",
        "apt-daily-upgrade.timer",
    ):
        live_unit(node, unit)
    (node.root / "var/lib/node-exporter-textfile").mkdir(parents=True)
    for path in package.activations:
        target = node.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(package.files[path][0] if path in package.files else b"")
    for edit in package.lines:
        target = node.root / edit["path"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as handle:
            handle.write(edit["line"] + "\n")
    node.state["users"] = {
        "node_exporter": 999
    }  # the account the live Pi already holds
    node.state["installed"]["homelab-daniel-pi"] = ["ii ", V1]
    node.state["ufw"] = {"active": True, "rules": [["limit 22/tcp", ""]], "fail": []}

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("ufw") == []
    # The sysctl edits share one file, so its changed hash reloads sysctl once.
    assert len(node.calls("sysctl")) == 1
    assert node.applied()["result"]["ok"] is True
