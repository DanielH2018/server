"""`hosts/lib/homelab-updater`'s fetch, install, textfile and `dpkg --audit` cases, against stubs.

Each case is one of H3's *Verification* updater cases, as a red and green pair.
`_updater_host.py` is the fake host.

Run: uv run pytest hosts/lib/tests/test_homelab_updater_fetch.py
"""

import os
import stat

import pytest
from _updater_host import EARLIER, V1, V2, Host


# Nothing found never reads as nothing newer.


@pytest.mark.parametrize(
    ("repo", "fails"),
    [
        (None, True),
        ({"homelab-updater": [V1]}, True),
        ({"homelab-updater": [V1], "homelab-daniel-pi": [V1]}, False),
    ],
    ids=["no-list", "no-host-stanza", "both-stanzas"],
)
def test_a_list_without_both_packages_exits_non_zero_and_writes_nothing(
    host, repo, fails
):
    host.succeeded_before()
    host.state["repo"] = repo

    result = host.run()

    assert result.returncode == (1 if fails else 0), result.stderr
    # The run reached step 4: it fetched, then read the list it fetched.
    assert ["apt-get", "indextargets"] == host.calls("apt-get")[1][:2]
    assert "--error-on=any" in host.calls("apt-get")[0]
    assert (
        "names no homelab-updater or no homelab-daniel-pi" in result.stderr
    ) is fails
    assert host.prom.exists() is not fails
    assert (host.last_success.read_text().strip() == EARLIER) is fails


# A failed fetch over a list an earlier good fetch left.


@pytest.mark.parametrize(("update_exit", "fails"), [(100, True), (0, False)])
def test_a_failed_homelab_fetch_exits_non_zero_with_no_metric_and_no_hold(
    host, update_exit, fails
):
    host.run()  # The earlier good fetch writes the list.
    host.prom.unlink()
    host.succeeded_before()
    host.state["update_exit"] = update_exit
    host.reconciled_at(V2)

    result = host.run()

    assert (result.returncode != 0) is fails
    assert host.prom.exists() is not fails
    assert not host.hold.exists()
    assert (host.last_success.read_text().strip() == EARLIER) is fails


def test_only_the_restricted_fetch_carries_error_on_any(host):
    host.publish(V2)
    host.state["install_failures"] = 1

    result = host.run()

    assert result.returncode == 0, result.stderr
    updates = [call for call in host.calls("apt-get") if call[1] == "update"]
    assert len(updates) == 2
    restricted, full = updates
    assert "--error-on=any" in restricted
    assert "Dir::Etc::sourcelist=sources.list.d/homelab.sources" in restricted
    assert full == ["apt-get", "update"]
    assert len(host.installs()) == 2


# The full apt-get update runs at most once a day, never between 04:00 and 05:59.


def _full_updates(host: Host) -> int:
    return sum(call == ["apt-get", "update"] for call in host.calls("apt-get"))


def test_a_second_dependency_failure_in_one_day_runs_no_second_full_update(host):
    host.publish(V2)
    host.state["install_failures"] = 2

    assert host.run().returncode == 1
    assert _full_updates(host) == 1

    host.state["install_failures"] = 1
    assert host.run().returncode == 1
    assert _full_updates(host) == 0

    host.state["today"] = "2026-10-14"
    host.state["install_failures"] = 1
    assert host.run().returncode == 0
    assert _full_updates(host) == 1


@pytest.mark.parametrize(("hour", "full_updates"), [("04", 0), ("05", 0), ("06", 1)])
def test_no_full_update_runs_in_the_apt_timers_hours(host, hour, full_updates):
    host.publish(V2)
    host.state["install_failures"] = 1
    host.state["hour"] = hour

    result = host.run()

    assert _full_updates(host) == full_updates
    assert len(host.installs()) == 1 + full_updates
    assert result.returncode == (1 if full_updates == 0 else 0), result.stderr
    assert ("between 04:00 and 05:59" in result.stderr) is (full_updates == 0)


@pytest.mark.parametrize(
    ("output", "full_updates"),
    [
        (
            "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 7",
            0,
        ),
        ("E: Sub-process /usr/bin/dpkg returned an error code (1)", 0),
        ("homelab-daniel-pi : Depends: tree but it is not installable", 1),
        (
            "E: Failed to fetch http://ports.ubuntu.com/pool/main/t/tree/tree.deb  404",
            1,
        ),
    ],
    ids=["lock-timeout", "dpkg-error", "unresolved-depends", "fetch-failure"],
)
def test_only_a_dependency_failure_earns_the_full_update(host, output, full_updates):
    host.publish(V2)
    host.state["install_failures"] = 1
    host.state["install_output"] = output

    result = host.run()

    assert _full_updates(host) == full_updates
    assert len(host.installs()) == 1 + full_updates
    assert ("not on a dependency" in result.stderr) is (full_updates == 0)
    # A run that spent nothing keeps the day's full update for a dependency failure.
    assert host.state_dir.joinpath("full-update").exists() is bool(full_updates)


def test_the_step_6_retry_waits_for_no_lock(host):
    host.publish(V2)
    host.state["install_failures"] = 1

    assert host.run().returncode == 0
    first, retry = host.installs()
    assert "DPkg::Lock::Timeout=600" in first
    assert "DPkg::Lock::Timeout=0" in retry


# The textfile directory.


def test_a_missing_textfile_directory_exits_non_zero_and_is_not_created(host):
    host.textfile.rmdir()

    result = host.run()

    assert result.returncode == 1
    assert not host.textfile.exists()


def test_the_metrics_file_is_written_mode_0644(host):
    # A bare mktemp file is 0600, which the exporter's user cannot read.
    umask = os.umask(0o077)
    try:
        host.run()
    finally:
        os.umask(umask)

    assert stat.S_IMODE(host.prom.stat().st_mode) == 0o644
    assert [path.name for path in host.textfile.iterdir()] == ["homelab_host.prom"]


def test_the_verify_count_is_the_line_count_of_dpkg_verify(host):
    host.state["verify"] = "??5?????? c /usr/lib/homelab/homelab-updater\nmissing /x\n"

    host.run()

    assert host.metrics()["homelab_host_verify_mismatches"].endswith(" 2")


# Step 2: dpkg --audit.


def test_a_package_dpkg_audit_lists_is_configured_and_reconciled(host):
    host.state["audit"] = "The following packages are only half configured\n"

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert ["dpkg", "--configure", "-a"] in host.calls("dpkg")
    assert host.reconciles() == 1


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (
            "dpkg: error: dpkg frontend lock was locked by another process with pid 7\n",
            75,
        ),
        ("dpkg: error processing package homelab-daniel-pi (--configure)\n", 1),
    ],
)
def test_a_held_frontend_lock_exits_75_and_writes_nothing(host, output, code):
    host.state["audit"] = "The following packages are only half configured\n"
    host.state["configure_exit"] = 2
    host.state["configure_output"] = output

    result = host.run()

    assert result.returncode == code
    assert not host.prom.exists()
    assert host.calls("apt-get") == []
