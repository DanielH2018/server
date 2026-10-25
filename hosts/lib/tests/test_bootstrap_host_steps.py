"""`hosts/bootstrap-host.sh` steps 4 to 6 on a fake host: the Pi purge, the k3s install in the
declared mode, and the local build from a clone, online and from the offline kit.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_steps.py
"""

import subprocess

import pytest
from _bootstrap_host import NFPM_ARCHIVE, FakeHost

AGENT_ENV = {
    "K3S_URL": "https://server.invalid:6443",
    "K3S_TOKEN": "secret-token-value",
}


def _server(tmp_path) -> FakeHost:
    host = FakeHost(tmp_path, hostname="daniel-server")
    host.stock_kit()
    return host


def test_the_pi_purges_only_what_is_installed_then_starts_the_engine(tmp_path):
    host = FakeHost(tmp_path)
    host._stub(
        "dpkg-query",
        "case $3 in snapd|fwupd) echo 'install ok installed' ;; *) exit 1 ;; esac",
    )
    host._stub("systemctl", f'echo "systemctl $*" >> {host.log}; [ "$1" != is-failed ]')

    result = host.run("daniel-pi")

    assert result.returncode == 0, result.stderr
    purge = [call for call in host.apt_calls() if call.startswith("purge")]
    assert purge == ["purge -y --autoremove snapd fwupd"]
    assert "systemctl start homelab-docker-engine.service" in host.commands()
    assert not (host.root / "installer.runs").exists()


def test_the_pi_skips_the_engine_when_no_package_ships_its_unit_yet(tmp_path):
    host = FakeHost(tmp_path)
    host._stub(
        "systemctl",
        f'echo "systemctl $*" >> {host.log}; [ "$1" != cat ] && [ "$1" != is-failed ]',
    )

    result = host.run("daniel-pi")

    assert result.returncode == 0, result.stderr
    assert "step 6 starts nothing" in result.stdout
    assert "systemctl start homelab-docker-engine.service" not in host.commands()


def test_the_agent_installs_k3s_in_agent_mode_and_never_prints_the_token(tmp_path):
    host = _server(tmp_path)

    result = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert result.returncode == 0, result.stderr
    args = (host.root / "installer.args").read_text()
    assert args.startswith("agent v1.37.1+k3s1 skipenable=true skipstart=true")
    assert "url=https://server.invalid:6443 token=set" in args
    assert AGENT_ENV["K3S_TOKEN"] not in result.stdout + result.stderr
    assert (host.root / "etc/systemd/system/k3s-agent.service").exists()
    assert not (host.root / "etc/systemd/system/k3s.service").exists()


def test_a_rerun_with_the_binary_and_unit_in_place_never_runs_the_installer_again(
    tmp_path,
):
    host = _server(tmp_path)
    host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    second = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert second.returncode == 0, second.stderr
    assert (host.root / "installer.runs").read_text().splitlines() == ["run"]


@pytest.mark.parametrize("missing", ["K3S_URL", "K3S_TOKEN"])
def test_an_agent_without_its_join_credentials_is_refused_before_anything_is_written(
    tmp_path, missing
):
    host = _server(tmp_path)
    env = {k: v for k, v in AGENT_ENV.items() if k != missing}

    result = host.run("daniel-server", "--offline", str(host.kit), **env)

    assert result.returncode == 1
    assert f"{missing} is not set" in result.stderr
    assert host.written() == []


def test_the_server_installs_in_server_mode_even_with_agent_variables_exported(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    clone = host.clone(tmp_path)

    result = host.run(
        "daniel-box",
        "--local-build",
        str(clone),
        "--offline",
        str(host.kit),
        **AGENT_ENV,
    )

    assert result.returncode == 0, result.stderr
    args = (host.root / "installer.args").read_text()
    assert args.startswith("server v1.37.1+k3s1")
    assert args.strip().endswith("url=unset token=")


@pytest.mark.parametrize(
    ("hostname", "env"),
    [("daniel-server", AGENT_ENV), ("daniel-box", {})],
    ids=["agent", "server"],
)
def test_an_installer_that_leaves_the_other_modes_unit_is_refused(
    tmp_path, hostname, env
):
    host = FakeHost(tmp_path, hostname=hostname)
    host.stock_kit()

    result = host.run(
        hostname, "--offline", str(host.kit), "--until", "4", WRONG_UNIT="1", **env
    )

    assert result.returncode == 1
    assert "does not exist after the installer" in result.stderr


def test_the_local_build_installs_three_packages_in_one_call_and_leaves_the_clone_alone(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    clone = host.clone(tmp_path)

    result = host.run(
        "daniel-box", "--local-build", str(clone), "--offline", str(host.kit)
    )

    assert result.returncode == 0, result.stderr
    assert "the clone at" in result.stdout and "is unchecked" in result.stdout
    [install] = [call for call in host.apt_calls() if call.startswith("install")]
    debs = install.split()[2:]
    assert [d.rsplit("/", 1)[1].split("_")[0] for d in debs] == [
        "homelab-tools",
        "homelab-updater",
        "homelab-daniel-box",
    ]
    assert (host.root / "nfpm.versions").read_text().split()[0].startswith("0.1+g")
    assert not (host.root / "etc/apt/sources.list.d/homelab.sources").exists()
    status = subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_the_online_local_build_asks_the_anchor_and_a_refusal_installs_nothing(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    clone = host.clone(tmp_path)
    # Online, install.sh and the archives come through the curl stub from the same directory.

    result = host.run("daniel-box", "--local-build", str(clone), ANCHOR_EXIT="1")

    assert result.returncode == 1
    assert "not a green first-parent commit of master" in result.stderr
    assert "bootstrap_anchor.py" in (host.root / "uv.calls").read_text()
    assert [c for c in host.apt_calls() if c.startswith("install")] == []


def test_a_kit_without_the_k3s_binary_is_refused_naming_the_file(tmp_path):
    host = _server(tmp_path)
    (host.kit / "k3s").unlink()

    result = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert result.returncode == 1
    assert "the offline kit has no k3s" in result.stderr


def test_a_kit_whose_nfpm_differs_from_its_pin_is_refused_naming_the_file(tmp_path):
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    (host.kit / NFPM_ARCHIVE).write_bytes(b"not the pinned nfpm")
    clone = host.clone(tmp_path)

    result = host.run(
        "daniel-box", "--local-build", str(clone), "--offline", str(host.kit)
    )

    assert result.returncode == 1
    assert f"{NFPM_ARCHIVE} does not match its pinned sha256" in result.stderr
    assert [c for c in host.apt_calls() if c.startswith("install")] == []
