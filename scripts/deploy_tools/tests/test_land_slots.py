"""The landing slots: when a landing holds one, how many there are, and how long it waits.

Run: uv run pytest scripts/deploy_tools/tests/test_land_slots.py
"""

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import slots, turn
from deploy_tools.promote_lib.wait import WaitResult
from lib.proc_testing import fake_bin, path_with

# A reset epoch decades away, so the quota always resets "far" from the real clock.
_RESETS_IN_2096 = 4_000_000_000
_SETTLED = WaitResult("settled", detail="1 applied", tags=("sonarr",))


def _names(calls):
    return [c[0] for c in calls]


def test_a_landing_holds_a_slot_from_the_master_ci_wait_to_its_verdict(land_run):
    rc, out, _, calls, _ = land_run([], Fakes(land_wait=[_SETTLED]))
    names = _names(calls)
    assert rc == 0 and "VERDICT: settled" in out
    assert names.index("try_slot") < names.index("await_ci")
    assert names.index("gate") < names.index("release_slot")
    assert out.index("== 2/") < out.index("took a landing slot after 0s")


def test_a_low_core_quota_lowers_the_count_to_1(land_run):
    quota = (100, _RESETS_IN_2096)
    _, _, _, calls, _ = land_run([], Fakes(land_wait=[_SETTLED], core_quota=quota))
    assert [c[1] for c in calls if c[0] == "try_slot"] == [(1,)]


def test_a_landing_waits_for_a_slot_only_as_long_as_its_merge_budgets_leave(
    landing, capsys
):
    ln, calls = landing(Fakes(slot=[False]), merge_timeout=60, merge_poll=30)
    ln.ledger.t_start = ln.tools.clock() - 50
    with slots.held(ln):
        pass
    slept = sum(c[1][0] for c in calls if c[0] == "sleep")
    # The budgets leave 2 * 60 - 51 seconds, as the fake clock ticks once more; a third poll
    # would pass them.
    assert slept == 60
    assert "going on without one" in capsys.readouterr().out


def test_a_landing_that_cannot_open_the_slots_goes_on_without_one(landing, capsys):
    ln, calls = landing(Fakes(slot=[OSError("read-only file system")]))
    with slots.held(ln):
        pass
    assert "sleep" not in _names(calls)
    assert "(read-only file system); going on without one" in capsys.readouterr().out


@pytest.mark.parametrize("hostname", [turn.TURN_HOST, "daniel-server"])
def test_a_cap_counts_every_held_slot_whichever_files_are_free(
    hostname, tmp_path, ssh_here, monkeypatch
):
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    paths = slots._paths(tmp_path)
    others = [slots.try_take([path], 1) for path in paths[1:3]]
    take = slots._taker(hostname, tmp_path)
    assert take(2) is None
    release = take(3)
    assert release
    assert slots.try_take(paths, 3) is None
    for held in (release, *others):
        if held:
            held()


def test_a_moved_turn_dir_keeps_the_slots_on_this_host(tmp_path, monkeypatch):
    stubs = fake_bin(tmp_path / "bin", ssh="#!/usr/bin/env bash\nexit 255\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.setenv(turn.TURN_DIR_ENV, str(tmp_path))
    release = slots._taker("daniel-server", tmp_path / "unmoved")(1)
    assert release
    assert slots.try_take(slots._paths(tmp_path), 1) is None
    release()


def test_a_turn_host_that_does_not_answer_leaves_this_host_its_own_slots(
    tmp_path, monkeypatch, capsys
):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.delenv(turn.TURN_DIR_ENV)
    take = slots._taker("daniel-server", tmp_path)
    held = [take(slots.COUNT) for _ in range(slots.FALLBACK_COUNT)]
    assert None not in held and take(slots.COUNT) is None
    assert capsys.readouterr().out.count("daniel-box did not answer") == 1
    for release in held:
        if release:
            release()


def test_a_slot_file_that_cannot_be_opened_leaves_the_others_free(tmp_path):
    free = tmp_path / "free.lock"
    with pytest.raises(FileNotFoundError):
        slots.try_take([free, tmp_path / "missing" / "slot.lock"], slots.COUNT)
    release = slots.try_take([free], 1)
    assert release
    release()
