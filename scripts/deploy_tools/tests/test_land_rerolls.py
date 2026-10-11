"""land_rerolls.later_deploys against real flocks and release records.

Run: uv run pytest scripts/deploy_tools/tests/test_land_rerolls.py

The locks are real flocks in a tmp directory (`HOMELAB_DEPLOY_LOCK_DIR`), held from a timer
thread the way a concurrent deploy would hold them. The cluster's workload generations come
from `_reads`, one canned answer per read, oldest first.
"""

import fcntl
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from deploy_tools.land_lib import land_rerolls


def _hold_then_release(path: Path, mode: int, after_s: float) -> threading.Thread:
    """Flock `path` as a deploy would, and release it `after_s` seconds later."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    fcntl.flock(fd, mode)
    holder = threading.Timer(after_s, os.close, (fd,))
    holder.start()
    return holder


def _reads(*answers):
    """A `generations` reader returning each of `answers` in turn."""
    pending = list(answers)
    return lambda: pending.pop(0)


def _workloads(**generations):
    """{workload: (generation, role label)}, one labelled Deployment per keyword."""
    return {
        ("Deployment", "default", name): (gen, name)
        for name, gen in generations.items()
    }


def test_later_deploys_waits_out_a_deploy_holding_the_service_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    (tmp_path / "server-deploy-all.lock").touch()
    (tmp_path / "server-deploy-sonarr.lock").touch()
    holder = _hold_then_release(
        tmp_path / "server-deploy-radarr.lock", fcntl.LOCK_EX, 0.3
    )
    (tmp_path / "radarr.json").write_text('{"commit": "c0ffee"}')
    reads = _reads(_workloads(radarr=4, sonarr=2), _workloads(radarr=5, sonarr=2))
    # The commit is read AFTER the wait, so it names what the later deploy rendered.
    assert land_rerolls.later_deploys(["radarr", "sonarr"], None, tmp_path, reads) == {
        "radarr": "c0ffee"
    }
    # It returned only once the holder had let go: a re-gate reads the finished rollout.
    assert not holder.is_alive()


def test_later_deploys_ignores_a_held_lock_that_left_the_generation_alone(
    tmp_path, monkeypatch
):
    """A deploy held the lock and rolled nothing, so the failed gate stands (#4514)."""
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    (tmp_path / "server-deploy-all.lock").touch()
    _hold_then_release(tmp_path / "server-deploy-radarr.lock", fcntl.LOCK_EX, 0.3)
    reads = _reads(_workloads(radarr=4), _workloads(radarr=4))
    assert land_rerolls.later_deploys(["radarr"], None, tmp_path, reads) == {}


def test_later_deploys_counts_only_the_tags_a_broad_apply_rolled(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    _hold_then_release(tmp_path / "server-deploy-all.lock", fcntl.LOCK_EX, 0.3)
    # sonarr is unlabelled and found through its release record's rollouts[] instead.
    (tmp_path / "sonarr.json").write_text(
        '{"rollouts": [{"kind": "deploy", "name": "sonarr"}]}'
    )
    before = {
        ("Deployment", "default", "radarr"): (4, "radarr"),
        ("Deployment", "media", "sonarr"): (2, ""),
    }
    after = {**before, ("Deployment", "media", "sonarr"): (3, "")}
    assert land_rerolls.later_deploys(
        ["radarr", "sonarr"], None, tmp_path, _reads(before, after)
    ) == {"sonarr": ""}


def test_later_deploys_reads_a_record_stamped_after_the_landings_deploy(
    tmp_path, monkeypatch
):
    """The deploy came and went before the check, so only its release record shows it."""
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    since = datetime(2026, 10, 9, 2, 16, 0, tzinfo=UTC).timestamp()
    (tmp_path / "radarr.json").write_text(
        '{"applied_at": "2026-10-09T02:16:36Z", "commit": "c0ffee"}'
    )
    (tmp_path / "sonarr.json").write_text('{"applied_at": "2026-10-09T02:15:59Z"}')
    reads = _reads(*[{}] * 3)
    assert land_rerolls.later_deploys(
        ["radarr", "sonarr", "lidarr"], since, tmp_path, reads
    ) == {"radarr": "c0ffee"}
    # No step-5 time, no comparison: a stamp alone proves nothing.
    assert land_rerolls.later_deploys(["radarr"], None, tmp_path, reads) == {}


def test_later_deploys_ignores_a_stamp_that_reapplied_the_same_manifests(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    since = datetime(2026, 10, 9, 2, 16, 0, tzinfo=UTC).timestamp()
    stamp = '"applied_at": "2026-10-09T02:16:36Z", "manifests_digest": "d1"'
    (tmp_path / "radarr.previous.json").write_text('{"manifests_digest": "d1"}')
    (tmp_path / "radarr.json").write_text("{" + stamp + "}")
    assert land_rerolls.later_deploys(["radarr"], since, tmp_path, _reads({})) == {}
    # The same manifests with a queued restart did roll the pods.
    (tmp_path / "radarr.json").write_text(
        "{" + stamp + ', "rollouts": [{"name": "radarr", "restart": true}]}'
    )
    assert land_rerolls.later_deploys(["radarr"], since, tmp_path, _reads({})) == {
        "radarr": ""
    }
