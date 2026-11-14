"""The hourly board resync fails its unit, and so pages, only on a streak of failed runs.

These tests run the rendered `board-sync` script with `uv` stubbed to exit with each run's
code, and a state directory standing in for systemd's `StateDirectory=`.

Run: uv run pytest hosts/tests/test_board_sync_paging.py
"""

import os
from pathlib import Path

from _shell_render import render_shell_script
from lib.proc_testing import run, write_exec

SYS_USER = "boarduser"


def _runs(tmp_path: Path, codes: list[int]) -> list[int]:
    """Run the script once per entry of `codes`, `sync-board` exiting with it; return its exits."""
    home = tmp_path / "home"
    (home / "server").mkdir(parents=True)
    rc_file = tmp_path / "rc"
    write_exec(home / ".local/bin/uv", f'exit "$(cat {rc_file})"\n')
    state = tmp_path / "state"
    state.mkdir()
    body = render_shell_script(
        "components", "board-sync", "board-sync.sh.j2", overrides={"sys_user": SYS_USER}
    )
    script = tmp_path / "board-sync"
    script.write_text(body.replace(f"/home/{SYS_USER}", str(home)))
    env = {**os.environ, "STATE_DIRECTORY": str(state)}
    exits = []
    for code in codes:
        rc_file.write_text(str(code))
        exits.append(run(["bash", str(script)], env=env).returncode)
    return exits


def test_a_failure_the_next_run_clears_never_fails_the_unit(tmp_path):
    assert _runs(tmp_path, [1, 1, 0, 1, 1, 0]) == [0, 0, 0, 0, 0, 0]


def test_a_lasting_failure_fails_the_unit_on_the_third_run_then_once_a_day(tmp_path):
    exits = _runs(tmp_path, [1] * 51)
    assert [n for n, rc in enumerate(exits, 1) if rc] == [3, 27, 51]
