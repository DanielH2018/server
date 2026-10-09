"""land_rerolls.later_deploys against real flocks and release records.

Run: uv run pytest scripts/deploy_tools/tests/test_land_rerolls.py

The locks are real flocks in a tmp directory (`HOMELAB_DEPLOY_LOCK_DIR`), held from a timer
thread the way a concurrent deploy would hold them.
"""

import fcntl
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from deploy_tools import land_rerolls


def _hold_then_release(path: Path, mode: int, after_s: float) -> threading.Thread:
    """Flock `path` as a deploy would, and release it `after_s` seconds later."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
    fcntl.flock(fd, mode)
    holder = threading.Timer(after_s, os.close, (fd,))
    holder.start()
    return holder


def test_later_deploys_waits_out_a_deploy_holding_the_service_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    (tmp_path / "server-deploy-all.lock").touch()
    (tmp_path / "server-deploy-sonarr.lock").touch()
    holder = _hold_then_release(
        tmp_path / "server-deploy-radarr.lock", fcntl.LOCK_EX, 0.3
    )
    assert land_rerolls.later_deploys(["radarr", "sonarr"], None, tmp_path) == [
        "radarr"
    ]
    # It returned only once the holder had let go: a re-gate reads the finished rollout.
    assert not holder.is_alive()


def test_later_deploys_counts_every_tag_while_a_broad_apply_holds_all(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path))
    _hold_then_release(tmp_path / "server-deploy-all.lock", fcntl.LOCK_EX, 0.3)
    assert land_rerolls.later_deploys(["radarr", "sonarr"], None, tmp_path) == [
        "radarr",
        "sonarr",
    ]


def test_later_deploys_reads_a_record_stamped_after_the_landings_deploy(
    tmp_path, monkeypatch
):
    """The deploy came and went before the check, so only its release record shows it."""
    monkeypatch.setenv("HOMELAB_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    since = datetime(2026, 10, 9, 2, 16, 0, tzinfo=UTC).timestamp()
    (tmp_path / "radarr.json").write_text('{"applied_at": "2026-10-09T02:16:36Z"}')
    (tmp_path / "sonarr.json").write_text('{"applied_at": "2026-10-09T02:15:59Z"}')
    assert land_rerolls.later_deploys(
        ["radarr", "sonarr", "lidarr"], since, tmp_path
    ) == ["radarr"]
    # No step-5 time, no comparison: a stamp alone proves nothing.
    assert land_rerolls.later_deploys(["radarr"], None, tmp_path) == []
