"""Each k3s node lets `who` read the name-service files under Ubuntu 26.04's `who` profile.

The profile the `apparmor` package ships omits `abstractions/nameservice`, so every `who` run
logs a denial for /etc/passwd and /etc/nsswitch.conf in the audit log.
"""

import shutil
from pathlib import Path

import pytest
from _host_packages import HOSTS, package, shipped_text
from lib.proc_testing import run

OVERRIDE = "/etc/apparmor.d/local/who"
UBUNTU_PROFILE = Path("/etc/apparmor.d/who")
K3S_NODES = ("daniel-box", "daniel-server")
PARSER = shutil.which("apparmor_parser", path="/usr/sbin:/sbin") or ""


@pytest.mark.parametrize("host", HOSTS)
def test_only_the_k3s_nodes_ship_the_override(host):
    assert (OVERRIDE in package(host).files) == (host in K3S_NODES)


@pytest.mark.parametrize("host", K3S_NODES)
def test_a_changed_override_reloads_apparmor(host):
    assert package(host).activations[OVERRIDE] == "reload:apparmor.service"


@pytest.mark.skipif(
    not (PARSER and UBUNTU_PROFILE.exists()),
    reason="needs apparmor_parser and the who profile the apparmor package ships",
)
@pytest.mark.parametrize("host", K3S_NODES)
def test_the_profile_with_the_override_reads_the_name_service_files(host, tmp_path):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "who").write_text(shipped_text(host, OVERRIDE))
    profile = tmp_path / "who"
    profile.write_text(UBUNTU_PROFILE.read_text())

    result = run(
        [PARSER, "--preprocess", "--base", str(tmp_path), "-I", "/etc/apparmor.d"]
        + [str(profile)]
    )

    assert result.returncode == 0, result.stderr
    rules = {line.strip() for line in result.stdout.splitlines()}
    assert {"@{etc_ro}/passwd r,", "@{etc_ro}/nsswitch.conf r,"} <= rules
