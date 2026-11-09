"""The rkhunter baseline refresh after every package change, outside apt's lock (#7680).

rkhunter's own APT_AUTOGEN hook ran `rkhunter --propupd` inside apt's DPkg::Post-Invoke, so apt
held the dpkg lock for about 2 minutes on the Pi after every package change. The integrity-scans
component turns APT_AUTOGEN off and ships a hook that starts homelab-rkhunter-propupd.service
without blocking. The unit's script waits for apt to release the lock, refreshes, and refreshes
again when dpkg changed packages during the refresh, because a start while the unit is active
does nothing.

Run: uv run pytest ansible/tests/setup/test_rkhunter_propupd.py
"""

import json
import os
from pathlib import Path

import pytest

from lib.proc_testing import fake_bin, run, write_exec
from lib.repo_paths import REPO
from _host_packages import HOSTS, package, shipped_text

SCRIPT = (
    REPO / "hosts" / "components" / "integrity-scans" / "files" / "rkhunter-propupd"
)
HOOK = "/etc/apt/apt.conf.d/91homelab-rkhunter-propupd"
UNIT = "homelab-rkhunter-propupd.service"


@pytest.mark.parametrize("host", HOSTS)
def test_every_host_refreshes_from_its_own_hook_and_not_from_apt_autogen(host):
    manifest = json.loads(package(host).files["/usr/lib/homelab/manifest.json"][0])
    autogen = [
        e["line"]
        for e in manifest["lines"]["edits"]
        if e["path"] == "/etc/default/rkhunter"
    ]
    assert autogen == ['APT_AUTOGEN="false"']
    assert f"systemctl start --no-block {UNIT}" in shipped_text(host, HOOK)
    unit = shipped_text(host, f"/usr/lib/systemd/system/{UNIT}")
    assert "ExecStart=/usr/lib/homelab/rkhunter-propupd\n" in unit


def _refresh(tmp_path: Path, changes_during_refresh: int) -> tuple[list[str], str]:
    """Run the script against stubs; the first `changes_during_refresh` refreshes rewrite status.

    Returns the calls the stubs logged, in order, and the script's stdout.
    """
    calls = tmp_path / "calls"
    status = tmp_path / "status"
    status.write_text("Package: a\n")
    countdown = tmp_path / "countdown"
    countdown.write_text(str(changes_during_refresh))
    write_exec(tmp_path / "lib" / "homelab-reconcile", f'echo "wait $1" >> {calls}\n')
    # A rename, as dpkg writes its status file, so the inode changes.
    stub_bin = fake_bin(
        tmp_path / "bin",
        rkhunter=(
            f'echo "rkhunter $*" >> {calls}\n'
            f"n=$(cat {countdown})\n"
            f'if [ "$n" -gt 0 ]; then\n'
            f"  echo $((n - 1)) > {countdown}\n"
            f'  echo "Package: b$n" > {status}.new && mv {status}.new {status}\n'
            "fi\n"
        ),
    )
    result = run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "HOMELAB_LIB_DIR": str(tmp_path / "lib"),
            "DPKG_STATUS": str(status),
        },
        stub_bin=stub_bin,
        check=True,
    )
    return calls.read_text().splitlines(), result.stdout


def test_a_quiet_dpkg_gets_one_refresh_after_the_wait_for_apt(tmp_path):
    calls, stdout = _refresh(tmp_path, changes_during_refresh=0)
    assert calls == ["wait --wait-for-apt", "rkhunter --propupd --nolog --nocolors"]
    assert stdout.endswith("rkhunter baseline refreshed\n")


def test_a_dpkg_run_during_the_refresh_gets_a_second_refresh(tmp_path):
    calls, _ = _refresh(tmp_path, changes_during_refresh=1)
    assert calls == ["wait --wait-for-apt", "rkhunter --propupd --nolog --nocolors"] * 2
