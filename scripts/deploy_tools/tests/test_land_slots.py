"""The landing slots: when a landing holds one, how many there are, and how long it waits.

Run: uv run pytest scripts/deploy_tools/tests/test_land_slots.py
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from _land_fakes import Fakes
from _land_turn_fakes import MERGED, OPEN, START_VIEW, call_order
from deploy_tools.land_lib import batch, slots, turn
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.promote_lib.wait import WaitResult, WaitVerdict
from lib.proc_testing import fake_bin, path_with

# A reset epoch decades away, so the quota always resets "far" from the real clock.
_RESETS_IN_2096 = 4_000_000_000
_SETTLED = WaitResult(WaitVerdict.SETTLED, detail="1 applied", tags=("sonarr",))


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
    take = slots._locker(hostname, tmp_path)
    assert take(slots.SLOT_NAMES, 2) is None
    release = take(slots.SLOT_NAMES, 3)
    assert release
    assert slots.try_take(paths, 3) is None
    for held in (release, *others):
        if held:
            held()


def test_a_moved_turn_dir_keeps_the_slots_on_this_host(tmp_path, monkeypatch):
    stubs = fake_bin(tmp_path / "bin", ssh="#!/usr/bin/env bash\nexit 255\n")
    monkeypatch.setenv("PATH", path_with(stubs))
    monkeypatch.setenv(turn.TURN_DIR_ENV, str(tmp_path))
    release = slots._locker("daniel-server", tmp_path / "unmoved")(slots.SLOT_NAMES, 1)
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
    take = slots._locker("daniel-server", tmp_path)
    held = [take(slots.SLOT_NAMES, slots.COUNT) for _ in range(slots.FALLBACK_COUNT)]
    assert None not in held and take(slots.SLOT_NAMES, slots.COUNT) is None
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


_K8S = ["k8s/roles/sonarr/defaults/main.yml"]
_SERIAL = ["k8s/roles/longhorn/defaults/main.yml"]
_SERVICES = ["cluster/services.yml"]
_DOCS = ["docs/landing.md"]


def _merging(landing, paths, draft=False, **fakes):
    """A landing of a PR changing `paths`, whose head reads green and merges."""
    views = {
        "state,mergeable,headRefOid": [OPEN, MERGED],
        START_VIEW: {"isDraft": draft, **OPEN},
        "files": {"files": [{"path": p} for p in paths]},
    }
    ln, calls = landing(Fakes(gh_views=views, **fakes))
    ln.direct_merge_subject = "Take turns"
    return ln, calls


def _land(ln) -> None:
    """The merge wait and its batch, as the pipeline runs them."""
    with turn.held(ln) as take_turn:
        batch.land(ln, take_turn)


@pytest.mark.parametrize(
    ("paths", "fakes", "why"),
    [
        (_K8S, {"deploy_hold": "abc123"}, "holds 'abc123', and this PR changes a k8s"),
        (_SERVICES, {"deploy_hold": "abc123"}, "holds 'abc123', and this PR changes"),
        (_SERIAL, {"serial": [False]}, f"another {slots._SERIAL_LIVE}"),
        (_DOCS, {"serial": [False]}, f"a {slots._SERIAL_LIVE}"),
        (_K8S, {"in_window": True, "slot": [False]}, "no-promotion window"),
    ],
    ids=[
        "hold",
        "services-yml-under-a-hold",
        "serial-live-past-the-drain",
        "beside-serial",
        "window",
    ],
)
def test_a_rule_refuses_the_merge_with_exit_75(landing, paths, fakes, why):
    ln, calls = _merging(landing, paths, **fakes)
    with pytest.raises(Outcome) as stop:
        _land(ln)
    assert stop.value.rc == 75 and why in stop.value.error
    assert "merge" not in call_order(calls)


@pytest.mark.parametrize(
    ("draft", "fakes"),
    [
        (False, {"slot": [False, False, True]}),
        (False, {"serial": [False, True]}),
        (True, {"slot": [False, False, False, True]}),
    ],
    ids=["a-slot-frees", "the-serial-lock-frees", "a-draft-beside-a-slot"],
)
def test_a_serial_pr_in_the_turn_merges_once_the_landings_past_their_merge_finish(
    landing, draft, fakes
):
    ln, calls = _merging(landing, _SERIAL, draft, **fakes)
    _land(ln)
    assert "merge" in call_order(calls)
    assert "sleep" in _names(calls)


@pytest.mark.parametrize(
    ("in_turn", "fakes"),
    [
        (False, {"slot": [False]}),
        (True, {"slot": [False], "in_window": True}),
    ],
    ids=["out-of-the-turn", "inside-the-window"],
)
def test_a_serial_pr_the_drain_cannot_clear_is_refused_without_waiting(
    landing, in_turn, fakes
):
    ln, calls = _merging(landing, _SERIAL, **fakes)
    with pytest.raises(Outcome) as stop:
        slots.refuse_before_merge(ln, in_turn)
    assert stop.value.rc == 75
    assert "sleep" not in _names(calls)


@pytest.mark.parametrize(
    ("paths", "draft", "fakes"),
    [
        (_DOCS, False, {"deploy_hold": "abc123"}),
        (_K8S, False, {"in_window": True}),
        (_SERIAL, True, {}),
    ],
    ids=["docs-under-a-hold", "window-with-no-slot-held", "a-serial-draft-alone"],
)
def test_a_pr_no_rule_bars_merges(landing, paths, draft, fakes):
    ln, calls = _merging(landing, paths, draft, **fakes)
    _land(ln)
    assert "merge" in call_order(calls)


def test_a_serial_landing_keeps_the_serial_lock_past_its_checks(landing):
    ln, _ = _merging(landing, _SERIAL)
    ln.tools = replace(ln.tools, try_slot=None)
    slots.refuse_before_merge(ln, in_turn=True)
    serial = Path(os.environ[turn.TURN_DIR_ENV]) / slots.SERIAL_NAME
    assert slots.try_take([serial], 1) is None
    assert ln.serial_lock
    ln.serial_lock()


def test_a_draft_a_rule_bars_spends_no_run(landing):
    ln, calls = _merging(landing, _K8S, draft=True, deploy_hold="abc123")
    with pytest.raises(Outcome) as stop:
        _land(ln)
    assert stop.value.rc == 75
    assert "pr ready" not in call_order(calls)


def test_the_checks_leave_the_lock_files_free(landing):
    ln, _ = _merging(landing, _K8S, in_window=True)
    ln.tools = replace(ln.tools, try_slot=None)
    slots.refuse_before_merge(ln, in_turn=True)
    moved = Path(os.environ[turn.TURN_DIR_ENV])
    names = [*slots.SLOT_NAMES, slots.SERIAL_NAME]
    release = slots.try_take([moved / n for n in names], 1)
    assert release
    release()
