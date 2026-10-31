"""Every fan-out batch unit carries its own memory, swap and pids limits (ADR-0027).

Run: uv run pytest scripts/dev/tests/test_fanout_batch_caps.py
"""

import shlex

from fanout_lib.launch import BATCH_MEMORY_MAX, systemd_run_command
from fanout_lib.placement import RESERVATION_BYTES
from fanout_lib.target import Target

DOTFILES = Target("DanielH2018/dotfiles", "/srv/dotfiles", "origin/main")


def _props(command: str) -> dict[str, str]:
    argv = shlex.split(command)
    # `claude -p` is the print flag, not a unit property, so keep only `-p Key=Value`.
    pairs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-p"]
    return dict(p.split("=", 1) for p in pairs if "=" in p)


def test_every_launch_shape_carries_the_batch_limits():
    for command in (
        systemd_run_command("b"),
        systemd_run_command("b", review=True),
        systemd_run_command("763", DOTFILES),
    ):
        props = _props(command)
        assert props["MemoryMax"] == str(BATCH_MEMORY_MAX)
        # A MemoryHigh at or above MemoryMax would never throttle before the kill.
        assert int(props["MemoryHigh"]) < BATCH_MEMORY_MAX
        assert props["MemorySwapMax"] == "512M"
        assert props["TasksMax"] == "1024"


def test_the_memory_limit_sits_above_the_placement_reservation():
    # A limit at or under the reservation would kill batches that placement priced as normal.
    assert BATCH_MEMORY_MAX % RESERVATION_BYTES == 0
    assert BATCH_MEMORY_MAX > RESERVATION_BYTES
