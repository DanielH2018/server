"""`hosts/bootstrap-host.sh` step 4 on a cluster node: the k3s install in the declared mode, and
the retry of a failed installer.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_k3s.py
"""

import pytest
from _bootstrap_host import AGENT_ENV, FakeHost, stocked_server
from renderer.renderer_lib import host as host_mode


def test_the_agent_installs_k3s_in_agent_mode_and_never_prints_the_token(tmp_path):
    host = stocked_server(tmp_path)

    result = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert result.returncode == 0, result.stderr
    args = (host.root / "installer.args").read_text()
    assert args.startswith("agent v1.37.2+k3s1 skipenable=true skipstart=true")
    assert "url=https://server.invalid:6443 token=set" in args
    assert AGENT_ENV["K3S_TOKEN"] not in result.stdout + result.stderr
    assert (host.root / "etc/systemd/system/k3s-agent.service").exists()
    assert not (host.root / "etc/systemd/system/k3s.service").exists()


def test_a_rerun_with_the_binary_and_unit_in_place_never_runs_the_installer_again(
    tmp_path,
):
    host = stocked_server(tmp_path)
    host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    second = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert second.returncode == 0, second.stderr
    assert (host.root / "installer.runs").read_text().splitlines() == ["run"]


@pytest.mark.parametrize("missing", ["K3S_URL", "K3S_TOKEN"])
def test_an_agent_without_its_join_credentials_is_refused_before_anything_is_written(
    tmp_path, missing
):
    host = stocked_server(tmp_path)
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
    assert args.startswith("server v1.37.2+k3s1")
    assert args.strip().endswith("url=unset token=")


def test_the_script_leaves_the_unit_stopped_and_says_the_operator_starts_it(tmp_path):
    host = stocked_server(tmp_path)

    result = host.run("daniel-server", "--offline", str(host.kit), **AGENT_ENV)

    assert result.returncode == 0, result.stderr
    assert (
        "k3s-agent.service stays stopped until the operator starts it" in result.stdout
    )
    assert not [c for c in host.commands() if c.startswith("systemctl") and "k3s" in c]


@pytest.mark.parametrize(
    ("node", "unit"),
    [("daniel-box", "k3s.service"), ("daniel-server", "k3s-agent.service")],
)
def test_no_cluster_package_ships_an_enable_link_for_its_k3s_unit(node, unit):
    links = host_mode.render_host(node).enable_links()

    assert "homelab-updater.timer" in links, "this reads some other list of links"
    assert unit not in links


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


@pytest.mark.parametrize(
    ("hostname", "env", "unit"),
    [
        ("daniel-server", AGENT_ENV, "k3s-agent.service"),
        ("daniel-box", {}, "k3s.service"),
    ],
    ids=["agent", "server"],
)
def test_an_installer_that_fails_once_is_run_again_and_the_bootstrap_continues(
    tmp_path, hostname, env, unit
):
    host = FakeHost(tmp_path, hostname=hostname)
    host.stock_kit()
    host._stub("sleep", f'echo "sleep $*" >> {host.log}')

    result = host.run(
        hostname, "--offline", str(host.kit), "--until", "4", FAIL_RUNS="1", **env
    )

    assert result.returncode == 0, result.stderr
    assert (host.root / "installer.runs").read_text().splitlines() == ["run", "run"]
    assert [c for c in host.commands() if c.startswith("sleep")] == ["sleep 30"]
    assert (host.root / "etc/systemd/system" / unit).exists()
    assert "stopped after step 4" in result.stdout


@pytest.mark.parametrize(
    ("hostname", "env"),
    [("daniel-server", AGENT_ENV), ("daniel-box", {})],
    ids=["agent", "server"],
)
def test_an_installer_that_stalls_is_killed_at_its_bound_and_run_again(
    tmp_path, hostname, env
):
    host = FakeHost(tmp_path, hostname=hostname)
    host.stock_kit()
    host.pin("K3S_INSTALLER_MAX_TIME=600", "K3S_INSTALLER_MAX_TIME=1")
    host._stub("sleep", f'echo "sleep $*" >> {host.log}')

    # capture_output waits for the pipes to close, so a surviving child also fails this run.
    result = host.run(
        hostname, "--offline", str(host.kit), "--until", "4", HANG_RUNS="1", **env
    )

    assert result.returncode == 0, result.stderr
    assert (host.root / "installer.runs").read_text().splitlines() == ["run", "run"]
    assert [c for c in host.commands() if c.startswith("sleep")] == ["sleep 30"]


def test_an_installer_that_always_fails_dies_after_the_last_attempt(tmp_path):
    host = stocked_server(tmp_path)
    host._stub("sleep", f'echo "sleep $*" >> {host.log}')

    result = host.run(
        "daniel-server", "--offline", str(host.kit), FAIL_RUNS="99", **AGENT_ENV
    )

    assert result.returncode == 1
    assert "bootstrap-host: the k3s installer failed\n" in result.stderr
    assert (host.root / "installer.runs").read_text().splitlines() == ["run"] * 13
    assert [c for c in host.commands() if c.startswith("sleep")] == ["sleep 30"] * 12
    assert host.apt_calls() == []
