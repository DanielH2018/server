"""`hosts/lib/homelab-updater`'s verdicts, holds and `--clear-hold` levers, against stubs.

Each case is one of H3's *Verification* updater cases, as a pair: the run the design asks for,
and the same run with the stub answering the other way. `_updater_host.py` is the fake host,
and `test_homelab_updater_fetch.py` holds the fetch, install and textfile cases.

Run: uv run pytest hosts/lib/tests/test_homelab_updater.py
"""

import pytest
from _updater_host import EARLIER, NOW, V1, V2, Host
from _updater_host import reconcile_ok as _ok


# Common path.


def test_a_reconciled_host_on_the_common_path_reports_success_without_reconcile(host):
    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.reconciles() == 0
    assert host.installs() == []
    metrics = host.metrics()
    assert _ok(metrics) == "1"
    assert metrics["homelab_host_applied_info"] == (
        f'{{host="daniel-pi",sha="aaaaaaaaaaaa",version="{V1}"}} 1'
    )
    assert metrics["homelab_host_update_success_timestamp_seconds"].endswith(f" {NOW}")
    assert host.last_success.read_text().strip() == NOW
    assert not host.hold.exists()


def test_a_record_naming_other_versions_starts_reconcile_before_any_metric(host):
    host.reconciled_at(V2)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.reconciles() == 1
    assert _ok(host.metrics()) == "1"


def test_a_failed_record_without_a_hold_starts_reconcile_and_a_second_failure_holds(
    host,
):
    host.succeeded_before()
    host.reconciled_at(V1, ok=False, earlier_ok="0.99+gcccccccccccc")
    host.state["reconcile"] = "fail"

    result = host.run()

    assert result.returncode == 1
    assert host.reconciles() == 1
    assert host.hold.read_text() == f"homelab-updater {V1}\nhomelab-daniel-pi {V1}\n"
    metrics = host.metrics()
    assert _ok(metrics) == "0"
    assert 'sha="cccccccccccc"' in metrics["homelab_host_applied_info"]
    assert metrics["homelab_host_update_success_timestamp_seconds"].endswith(
        f" {EARLIER}"
    )


# A failed reconcile holds the host.


def test_a_failed_reconcile_after_an_install_holds_and_advances_nothing(host):
    host.succeeded_before()
    host.publish(V2)
    host.state["reconcile"] = "fail"

    result = host.run()

    assert result.returncode == 1
    assert [call[-2:] for call in host.installs()] == [
        ["homelab-updater", "homelab-daniel-pi"]
    ]
    assert host.hold.exists()
    metrics = host.metrics()
    assert _ok(metrics) == "0"
    assert f'version="{V1}"' in metrics["homelab_host_applied_info"]
    assert metrics["homelab_host_update_success_timestamp_seconds"].endswith(
        f" {EARLIER}"
    )
    assert host.last_success.read_text().strip() == EARLIER


def test_a_passing_reconcile_after_an_install_advances_the_applied_version(host):
    host.publish(V2)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f'version="{V2}"' in host.metrics()["homelab_host_applied_info"]
    assert not host.hold.exists()


def test_a_held_host_installs_nothing_and_reports_reconcile_ok_0(host):
    host.state_dir.mkdir(parents=True)
    host.hold.write_text("held\n")
    host.publish(V2)

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert host.installs() == []
    assert host.reconciles() == 0
    assert host.calls("apt-get") == []
    assert _ok(host.metrics()) == "0"


def test_a_host_killed_after_its_install_runs_reconcile_on_the_next_common_path(host):
    # The install landed V2 and the run died before step 7, so the record still names V1.
    host.state["installed"] = {
        "homelab-updater": ["ii ", V2],
        "homelab-daniel-pi": ["ii ", V2],
    }
    host.publish(V2)
    host.state["reconcile"] = "fail"

    result = host.run()

    assert result.returncode == 1
    assert host.installs() == []
    assert host.reconciles() == 1
    assert _ok(host.metrics()) == "0"
    assert host.hold.exists()


# Both --clear-hold levers.


def _held_on_a_failed_restart(host: Host) -> None:
    host.succeeded_before()
    host.reconciled_at(V1, ok=False)
    host.hold.write_text(f"homelab-updater {V1}\nhomelab-daniel-pi {V1}\n")


def test_clear_hold_reruns_reconcile_and_clears_the_hold_when_it_passes(host):
    _held_on_a_failed_restart(host)

    result = host.run("--clear-hold")

    assert result.returncode == 0, result.stderr
    # The stub's start is the rerun that retries the failed restart; an empty diff would skip it.
    assert host.reconciles() == 1
    assert not host.hold.exists()
    assert _ok(host.metrics()) == "1"


def test_clear_hold_keeps_the_hold_and_installs_nothing_while_reconcile_fails(host):
    _held_on_a_failed_restart(host)
    host.publish(V2)
    host.state["reconcile"] = "fail"

    result = host.run("--clear-hold")

    assert result.returncode == 1
    assert host.reconciles() == 1
    assert host.calls("apt-get") == []
    assert host.hold.exists()
    assert _ok(host.metrics()) == "0"


def test_install_newest_installs_the_fix_and_clears_the_hold_when_reconcile_passes(
    host,
):
    _held_on_a_failed_restart(host)
    host.publish(V2)

    result = host.run("--clear-hold", "--install-newest")

    assert result.returncode == 0, result.stderr
    assert host.state["installed"]["homelab-daniel-pi"] == ["ii ", V2]
    assert host.reconciles() == 1
    assert not host.hold.exists()
    assert f'version="{V2}"' in host.metrics()["homelab_host_applied_info"]


def test_install_newest_writes_the_hold_again_when_reconcile_fails(host):
    _held_on_a_failed_restart(host)
    host.publish(V2)
    host.state["reconcile"] = "fail"

    result = host.run("--clear-hold", "--install-newest")

    assert result.returncode == 1
    assert host.state["installed"]["homelab-daniel-pi"] == ["ii ", V2]
    assert host.hold.read_text() == f"homelab-updater {V2}\nhomelab-daniel-pi {V2}\n"


def test_an_unknown_argument_is_a_usage_error(host):
    assert host.run("--clear").returncode == 64
    assert [call[0] for call in host.calls()] == ["hostname"]


# A package that is not "ii ".


@pytest.mark.parametrize(("status", "reinstalled"), [("iU ", True), ("ii ", False)])
def test_a_half_installed_package_is_reinstalled(host, status, reinstalled):
    host.state["installed"]["homelab-daniel-pi"] = [status, V1]

    host.run()

    reinstalls = [call for call in host.installs() if "--reinstall" in call]
    assert bool(reinstalls) is reinstalled
    if reinstalled:
        assert reinstalls[0][-1] == "homelab-daniel-pi"
        assert host.reconciles() == 1


@pytest.mark.parametrize(
    ("args", "held"),
    [(["--clear-hold", "--install-newest"], False), (["--clear-hold"], True)],
)
def test_install_newest_removes_the_hold_before_its_fetch(host, args, held):
    # The operator asked for the hold to go, so a failed fetch leaves the next timer run free
    # to install. --clear-hold alone fetches nothing and keeps a hold reconcile does not clear.
    _held_on_a_failed_restart(host)
    host.state["update_exit"] = 100
    host.state["reconcile"] = "fail"

    host.run(*args)

    assert host.hold.exists() is held
