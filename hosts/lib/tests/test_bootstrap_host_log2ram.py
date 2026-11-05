"""`hosts/bootstrap-host.sh` step 5's log2ram install on a fake host.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_log2ram.py
"""

from _bootstrap_host import FakeHost


def test_a_fresh_pi_installs_log2ram_from_the_azlux_source_after_its_package(tmp_path):
    host = FakeHost(tmp_path)
    replaced = host.root / "etc/log2ram.conf.dpkg-old"
    replaced.parent.mkdir()
    replaced.touch()

    result = host.run("daniel-pi", "--until", "5")

    assert result.returncode == 0, result.stderr
    calls = host.apt_calls()
    package = calls.index("install -y homelab-updater homelab-daniel-pi")
    update, install = calls[package + 1 :]
    assert update.startswith("update --error-on=any ")
    assert "-o Dir::Etc::sourcelist=sources.list.d/azlux.sources" in update
    assert "-o Dir::Etc::sourceparts=-" in update
    assert "-o APT::Get::List-Cleanup=0" in update
    # Reconcile may have written /etc/log2ram.conf, and that file stops dpkg at a prompt.
    assert "-o Dpkg::Options::=--force-confnew" in install
    assert install.endswith(" log2ram")
    assert not replaced.exists()


def test_a_failed_azlux_update_stops_before_the_log2ram_install(tmp_path):
    host = FakeHost(tmp_path)
    host._stub(
        "apt-get",
        f'echo "$*" >> {host.calls}\ncase "$*" in *azlux.sources*) exit 100 ;; esac',
    )

    result = host.run("daniel-pi")

    assert result.returncode == 1
    assert "apt-get update of the Azlux source failed" in result.stderr
    assert not [call for call in host.apt_calls() if call.endswith(" log2ram")]


def test_a_pi_rerun_with_log2ram_installed_makes_no_azlux_call(tmp_path):
    host = FakeHost(tmp_path)
    host._stub(
        "dpkg-query",
        "case $3 in log2ram) echo 'install ok installed' ;; *) exit 1 ;; esac",
    )

    result = host.run("daniel-pi", "--until", "5")

    assert result.returncode == 0, result.stderr
    assert host.apt_calls()[-1] == "install -y homelab-updater homelab-daniel-pi"


def test_a_cluster_node_gets_no_log2ram(tmp_path):
    host = FakeHost(tmp_path, hostname="daniel-server")
    host.stock_kit()

    result = host.run(
        "daniel-server",
        "--until",
        "5",
        "--offline",
        str(host.kit),
        K3S_URL="https://server.invalid:6443",
        K3S_TOKEN="secret-token-value",
    )

    assert result.returncode == 0, result.stderr
    assert "install -y homelab-updater homelab-daniel-server" in host.apt_calls()
    assert not [
        call for call in host.apt_calls() if "log2ram" in call or "azlux" in call
    ]
