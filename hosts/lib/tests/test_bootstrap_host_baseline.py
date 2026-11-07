"""Step 4 of `hosts/bootstrap-host.sh` holds the host baseline no package can (H7.58).

The Pi's swap file comes before its first heavy apt. Every host gets the `ssh-users` group and the
sys user's membership before step 5 installs sshd's AllowGroups, and the networkd-dispatcher mask.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_baseline.py
"""

import pytest
from _bootstrap_host import AGENT_ENV, FakeHost, stocked_server


def test_the_pi_creates_its_swap_file_before_its_first_apt(tmp_path):
    host = FakeHost(tmp_path)
    host._stub(
        "dpkg-query",
        "case $3 in snapd) echo 'install ok installed' ;; *) exit 1 ;; esac",
    )
    host._stub("apt-get", f'echo "apt $*" >> {host.log}')

    result = host.run("daniel-pi", "--until", "4")

    assert result.returncode == 0, result.stderr
    swap = host.root / "swapfile"
    commands = host.commands()
    purge = next(i for i, c in enumerate(commands) if c.startswith("apt purge"))
    assert commands.index(f"swapon {swap}") < purge
    assert f"fallocate -l 1G {swap}.tmp" in commands
    assert f"mkswap {swap}.tmp" in commands
    assert (host.root / "etc/fstab").read_text() == "/swapfile none swap sw 0 0\n"
    assert swap.stat().st_mode & 0o777 == 0o600


def test_a_pi_rerun_keeps_its_swap_file_and_fstab_line_and_formats_nothing(tmp_path):
    host = FakeHost(tmp_path)
    swap = host.root / "swapfile"
    (host.root / "etc").mkdir(parents=True, exist_ok=True)
    swap.write_text("live swap")
    (host.root / "etc/fstab").write_text("/swapfile none swap sw 0 0\n")
    host._stub("swapon", f'[ "$1" != --show=NAME ] || echo {swap}')

    result = host.run("daniel-pi", "--until", "4")

    assert result.returncode == 0, result.stderr
    assert swap.read_text() == "live swap"
    assert (host.root / "etc/fstab").read_text() == "/swapfile none swap sw 0 0\n"
    assert not [c for c in host.commands() if c.split()[0] in {"mkswap", "swapon"}]


@pytest.mark.parametrize("hostname", ["daniel-pi", "daniel-server"])
def test_every_host_gets_ssh_users_and_the_networkd_dispatcher_mask_in_step_4(
    tmp_path, hostname
):
    host = (
        stocked_server(tmp_path) if hostname == "daniel-server" else FakeHost(tmp_path)
    )
    flags = ["--offline", str(host.kit)] if hostname == "daniel-server" else []

    result = host.run(hostname, "--until", "4", *flags, **AGENT_ENV)

    assert result.returncode == 0, result.stderr
    commands = host.commands()
    assert "groupadd ssh-users" in commands
    assert "usermod -aG ssh-users ubuntu" in commands
    assert "systemctl mask networkd-dispatcher.service" in commands


def test_a_host_already_holding_the_group_and_membership_changes_neither(tmp_path):
    host = FakeHost(tmp_path)
    host._stub("getent", "echo ssh-users:x:1001:ubuntu")
    host._stub("id", "echo ubuntu ssh-users")

    result = host.run("daniel-pi", "--until", "4")

    assert result.returncode == 0, result.stderr
    assert not [c for c in host.commands() if c.split()[0] in {"groupadd", "usermod"}]


def test_a_host_without_the_sys_user_is_refused_before_sshd_can_lock_it_out(tmp_path):
    host = FakeHost(tmp_path)
    host._stub("id", "exit 1")

    result = host.run("daniel-pi", "--until", "4")

    assert result.returncode == 1
    assert "no user ubuntu" in result.stderr
