"""The limit on live landings and each condition that lowers it (`land_lib/live_limit.py`).

Run: uv run pytest scripts/deploy_tools/tests/test_land_live_limit.py
"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from deploy_tools.land_lib import detach, live_limit

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
K8S_PATH = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"


def _iso(age: timedelta) -> str:
    return (NOW - age).isoformat().replace("+00:00", "Z")


QUIET = live_limit.Reads(
    pr_paths=lambda pr: ["scripts/dev/x.py"],
    core_quota=lambda: (5000, int(NOW.timestamp()) + 3600),
    master_runs=lambda: [],
    hold=lambda: "none",
    in_window=lambda now: False,
    now=lambda: NOW,
)


def _reads(**fields) -> live_limit.Reads:
    """Reads under which nothing lowers the limit, with `fields` replacing some."""
    return replace(QUIET, **fields)


def _limit(reads: live_limit.Reads, live: list[str] | None = None) -> tuple[int, str]:
    return live_limit.admission("939", reads).limit(live or [])


def test_the_default_limit_is_three():
    assert _limit(_reads()) == (3, "the default of 3")


def test_land_max_live_overrides_the_default(monkeypatch):
    monkeypatch.setenv(live_limit.MAX_LIVE_ENV, "5")
    assert _limit(_reads()) == (5, "LAND_MAX_LIVE=5")


def test_a_low_quota_far_from_its_reset_lowers_the_limit_to_one():
    reset = int(NOW.timestamp()) + 40 * 60
    cap, why = _limit(_reads(core_quota=lambda: (1234, reset)))
    assert cap == 1
    assert "core quota has 1234 calls left and resets in 40m" in why


def test_a_low_quota_about_to_reset_lowers_nothing():
    reset = int(NOW.timestamp()) + 10 * 60
    assert _limit(_reads(core_quota=lambda: (1234, reset)))[0] == 3


def test_two_slow_master_runs_lower_the_limit_to_two():
    runs = [{"status": "queued", "created_at": _iso(timedelta(minutes=20))}] * 2
    cap, why = _limit(_reads(master_runs=lambda: runs))
    assert cap == 2
    assert "2 master CI runs unfinished for over 15m" in why


def test_young_master_runs_lower_nothing():
    runs = [{"status": "queued", "created_at": _iso(timedelta(minutes=5))}] * 2
    assert _limit(_reads(master_runs=lambda: runs))[0] == 3


def test_deploy_hold_lowers_a_k8s_pr_to_zero():
    reads = _reads(pr_paths=lambda pr: [K8S_PATH], hold=lambda: "abc123")
    cap, why = _limit(reads)
    assert cap == 0
    assert "DEPLOY_HOLD holds 'abc123'" in why


def test_deploy_hold_does_not_lower_a_pr_with_no_k8s_change():
    assert _limit(_reads(hold=lambda: "abc123"))[0] == 3


def test_the_reboot_window_lowers_a_k8s_pr_to_one():
    reads = _reads(pr_paths=lambda pr: [K8S_PATH], in_window=lambda now: now == NOW)
    cap, why = _limit(reads)
    assert cap == 1
    assert "no-promotion window" in why


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/k8s/longhorn/defaults/main.yml",
        "ansible/roles/k8s/metallb/defaults/main.yml",
        "ansible/roles/k8s/flux/templates/sync.yaml.j2",
        "ansible/vars/cluster-vars.sops.env",
    ],
)
def test_a_serial_pr_lowers_its_own_limit_to_one(path):
    assert _limit(_reads(pr_paths=lambda pr: [path]))[0] == 1


def test_a_live_serial_landing_lowers_the_limit_to_one():
    cap, why = _limit(_reads(), live=[live_limit.SERIAL])
    assert cap == 1
    assert "pin, flux or cluster-vars change is live" in why


def test_a_failed_read_lowers_nothing():
    def refused():
        raise OSError("gh: API rate limit exceeded")

    reads = _reads(
        pr_paths=lambda pr: [K8S_PATH],
        core_quota=refused,
        master_runs=refused,
        hold=refused,
        in_window=lambda now: refused(),
    )
    assert _limit(reads)[0] == 3


def test_a_detach_under_a_low_quota_is_refused_naming_the_quota(monkeypatch, tmp_path):
    """#8142 verify-by: with one landing live and the quota low, the next `--detach` exits 75."""
    monkeypatch.setenv(detach.LIVE_DIR_ENV, str(tmp_path))
    (tmp_path / "default").mkdir()
    (tmp_path / "default" / f"{os.getpid()}.slot").write_text("")
    reset = int(NOW.timestamp()) + 40 * 60
    admission = live_limit.admission("939", _reads(core_quota=lambda: (900, reset)))

    with pytest.raises(detach.TooManyLandings, match="core quota has 900 calls left"):
        detach.fork(tmp_path / "land939.log", lambda: 0, admission)
