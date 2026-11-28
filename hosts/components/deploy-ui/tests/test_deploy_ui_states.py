"""The landing queue from the state records, written by land_lib's own record writer."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from deploy_tools.land_lib import line, serial_lock, slots, turn
from deploy_tools.land_lib.transitions import CHECKS, State, Transitions

import deploy_ui
from deploy_ui_lib import deploy_ui_reads as reads
from deploy_ui_lib import deploy_ui_states as states

NOW = 1_764_300_000.0


def _landing(directory, pr, host, entered):
    """A landing on `host` that entered each `(state, at)` in turn, through `Transitions`."""
    clock = SimpleNamespace(t=0.0)
    tools = SimpleNamespace(
        now=lambda: clock.t,
        logger=lambda _text: None,
        hostname=lambda: host,
        line=line.here(directory),
    )
    writer = Transitions(pr, tools)
    for state, at in entered:
        clock.t = at
        writer.enter(state)
    return writer


@pytest.fixture
def lock_dir(tmp_path):
    (tmp_path / "proc_locks").write_text("")
    (tmp_path / "run_lock").mkdir()
    return tmp_path / "run_lock"


@pytest.fixture
def app(tmp_path, lock_dir):
    cfg = deploy_ui.Config(
        repo=tmp_path,
        log_dir=tmp_path / "logs",
        bind="127.0.0.1",
        port=0,
        tree_lock=tmp_path / "tree.lock",
        proc_locks=tmp_path / "proc_locks",
        cli_log_dir=tmp_path / "cli",
        land_locks=lock_dir,
        hostname="daniel-box",
    )
    ok = subprocess.CompletedProcess([], 0, "", "")
    return deploy_ui.App(cfg, run=lambda argv, **kw: ok, now=lambda: NOW)


def test_inflight_lists_both_hosts_landings_with_state_progress_and_queue_is_clean(
    app, lock_dir
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    records = lock_dir / states.LINE_DIR_NAME
    holder = _landing(
        records,
        "101",
        "daniel-box",
        [
            (State.QUEUED, NOW - 300),
            (State.HOLDING, NOW - 200),
            (State.TESTING, NOW - 90),
        ],
    )
    holder.progress(4, 11, CHECKS)
    _landing(records, "202", "daniel-server", [(State.QUEUED, NOW - 60)])
    remote_lock = lock_dir / states.PR_LOCK.format(pr="202")
    remote_lock.touch()
    maj, mnr, ino = reads.lock_key(str(remote_lock))
    (lock_dir.parent / "proc_locks").write_text(
        f"9: FLOCK  ADVISORY  WRITE 4242 {maj:02x}:{mnr:02x}:{ino} 0 EOF\n"
    )
    got = [
        {k: row[k] for k in ("pr", "host", "state", "in_state_s", "progress", "queue")}
        | {"position": row["position"], "passed": [h["state"] for h in row["history"]]}
        for row in json.loads(app.get("/api/inflight")[1])["landings"]
    ]
    assert got == [
        {
            "pr": "101",
            "host": "daniel-box",
            "state": "testing",
            "in_state_s": 90,
            "progress": {"done": 4, "total": 11, "what": CHECKS},
            "queue": "turn",
            "position": None,
            "passed": ["queued", "holding"],
        },
        {
            "pr": "202",
            "host": "daniel-server",
            "state": "queued",
            "in_state_s": 60,
            "progress": None,
            "queue": "waiting",
            "position": 1,
            "passed": [],
        },
    ]


def test_waiting_landings_rank_by_when_they_queued_then_the_stepped_out_is_clean(
    tmp_path,
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    _landing(tmp_path, "30", "daniel-box", [(State.QUEUED, NOW - 10)])
    _landing(tmp_path, "10", "daniel-box", [(State.QUEUED, NOW - 50)])
    _landing(
        tmp_path,
        "40",
        "daniel-box",
        [(State.HOLDING, NOW - 90), (State.YIELDED, NOW - 80)],
    )
    _landing(tmp_path, "20", "daniel-box", [(State.QUEUED, NOW - 30)])
    rows = states.landing_rows(
        states.read_records(tmp_path, NOW).records, NOW, "daniel-box", tmp_path, {}
    )
    assert [(r["pr"], r["queue"], r["position"]) for r in rows] == [
        ("10", "waiting", 1),
        ("20", "waiting", 2),
        ("30", "waiting", 3),
        ("40", "stepped-out", None),
    ]


def test_a_landing_whose_process_is_gone_here_leaves_the_queue_is_flagged(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    _landing(tmp_path, "50", "daniel-box", [(State.QUEUED, NOW - 100)])
    records = states.read_records(tmp_path, NOW).records
    proc = tmp_path / "proc"
    proc.mkdir()
    [dead] = states.landing_rows(records, NOW, "daniel-box", tmp_path, {}, proc)
    assert (dead["alive"], dead["queue"], dead["position"]) == (False, "", None)
    [live] = states.landing_rows(records, NOW, "daniel-box", tmp_path, {})
    assert (live["alive"], live["queue"], live["position"]) == (True, "waiting", 1)


def test_a_landing_on_another_host_without_its_pr_lock_is_unknown_not_dead_is_flagged(
    tmp_path,
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    _landing(tmp_path, "55", "daniel-server", [(State.HOLDING, NOW - 100)])
    _landing(tmp_path, "56", "daniel-box", [(State.QUEUED, NOW - 50)])
    records = states.read_records(tmp_path, NOW).records
    proc = tmp_path / "proc"
    (proc / str(os.getpid())).mkdir(parents=True)
    rows = states.landing_rows(records, NOW, "daniel-box", tmp_path, {}, proc)
    assert [(r["pr"], r["alive"], r["queue"], r["position"]) for r in rows] == [
        ("56", True, "waiting", 1),
        ("55", None, "", None),
    ]
    table = _held_table(tmp_path, [states.PR_LOCK.format(pr="55")])
    rows = states.landing_rows(records, NOW, "daniel-box", tmp_path, table, proc)
    assert [(r["pr"], r["alive"], r["queue"]) for r in rows] == [
        ("55", True, "turn"),
        ("56", True, "waiting"),
    ]


def test_the_newest_record_of_a_pr_wins_and_a_verdict_leaves_the_queue_is_flagged(
    tmp_path,
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    writer = _landing(tmp_path, "70", "daniel-server", [(State.QUEUED, NOW - 100)])
    mine = tmp_path / f"70.{os.getuid()}.state"
    older = json.loads(mine.read_text())
    (tmp_path / f"70.{os.getuid() + 1}.state").write_text(
        json.dumps({**older, "since": NOW - 500})
    )
    (tmp_path / "80.1000.state").write_text("{not json")
    _landing(
        tmp_path, "90", "daniel-server", [(State.QUEUED, NOW - 3 * states.WINDOW_S)]
    )
    assert [(r.pr, r.since) for r in states.read_records(tmp_path, NOW).records] == [
        ("70", NOW - 100)
    ]
    writer.end("settled")
    assert states.read_records(tmp_path, NOW).records == []


def test_an_unreadable_record_is_named_and_the_others_still_show_is_flagged(
    app, lock_dir
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    records = lock_dir / states.LINE_DIR_NAME
    _landing(records, "11", "daniel-server", [(State.QUEUED, NOW - 30)])
    hidden = records / "12.4242.state"
    hidden.write_text("{}")
    hidden.chmod(0)
    got = json.loads(app.get("/api/inflight")[1])
    assert [r["pr"] for r in got["landings"]] == ["11"]
    assert got["landings_unavailable"] == (
        f"1 state record(s) in {states.LINE_DIR_NAME} cannot be read"
    )


def test_a_link_a_fifo_or_undecodable_bytes_at_a_records_name_is_skipped_is_flagged(
    app, lock_dir
):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    records = lock_dir / states.LINE_DIR_NAME
    _landing(records, "11", "daniel-box", [(State.QUEUED, NOW - 30)])
    elsewhere = lock_dir / "elsewhere.json"
    record = json.loads((records / f"11.{os.getuid()}.state").read_text())
    elsewhere.write_text(json.dumps({**record, "pr": "12"}))
    (records / "12.1000.state").symlink_to(elsewhere)
    os.mkfifo(records / "13.1000.state")
    (records / "14.1000.state").write_bytes(b"\xff\xfe")
    (records / "15.1000.state").write_bytes(b" " * (states.MAX_RECORD_BYTES + 1))
    got = json.loads(app.get("/api/inflight")[1])
    assert [r["pr"] for r in got["landings"]] == ["11"]
    assert got["landings_unavailable"] == ""


def test_every_landing_state_has_a_label_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    assert set(states.STATE_LABELS) == {s.value for s in State}


def test_a_state_the_page_does_not_know_renders_by_its_raw_name_is_flagged():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    assert states.label("a-newer-state") == "a-newer-state"


def test_the_lock_names_are_land_libs_is_clean():
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    assert states.LOCK_DIR / states.LINE_DIR_NAME == line.DIR
    assert states.LOCK_DIR == serial_lock.LOCK_DIR == turn.LOCK.parent
    assert (states.TURN_LOCK, states.SERIAL_LOCK, states.SERIAL_HOLDER) == (
        turn.LOCK.name,
        serial_lock.SERIAL_NAME,
        serial_lock.HOLDER_NAME,
    )
    assert states.SLOT_LOCKS == slots.SLOT_NAMES
    assert states.PR_LOCK.format(pr="12") == serial_lock.lock_name("12")


def _held_table(lock_dir, names):
    lock_dir.mkdir(exist_ok=True)
    for name in names:
        (lock_dir / name).touch()
    return {
        reads.lock_key(str(lock_dir / n)): reads.FileLock(True, frozenset())
        for n in names
    }


def test_turn_locks_name_the_serial_holder_and_count_the_slots_is_clean(tmp_path):
    # fact: hosts/components/deploy-ui/CLAUDE.md#Landing queue
    held = [states.SERIAL_LOCK, states.PR_LOCK.format(pr="321"), *states.SLOT_LOCKS[:2]]
    table = _held_table(tmp_path, held)
    (tmp_path / states.SERIAL_HOLDER).write_text("321\n")
    assert states.turn_locks(tmp_path, table) == {
        "turn_held": False,
        "serial": {"held": True, "pr": "321"},
        "slots": {"used": 2, "of": len(states.SLOT_LOCKS)},
    }


def test_a_serial_holder_whose_landing_is_gone_names_no_pr_is_flagged(tmp_path):
    table = _held_table(tmp_path, [states.TURN_LOCK, states.SERIAL_LOCK])
    (tmp_path / states.SERIAL_HOLDER).write_text("321\n")
    got = states.turn_locks(tmp_path, table)
    assert (got["turn_held"], got["serial"]) == (True, {"held": True, "pr": ""})
