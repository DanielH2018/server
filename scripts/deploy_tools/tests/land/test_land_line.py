"""The line for the landing turn: who stands in it, and what its holder answers.

Run: uv run pytest scripts/deploy_tools/tests/land/test_land_line.py
"""

import os

import pytest

from deploy_tools.land_lib import line
from lib.proc_testing import fake_bin, path_with

HEAD = "a" * 40
MOVED = "b" * 40


@pytest.fixture
def the_line(tmp_path):
    return line.here(tmp_path / "line")


def _waiting(the_line) -> list[str]:
    return the_line("waiting", str(line.FRESH_S)).splitlines()


def _answer(reply: str) -> str:
    """The answer in a `join` or `stand` reply."""
    return (reply.splitlines()[0].split()[1:] or [""])[0]


def test_a_landing_that_joins_is_in_the_line_until_it_leaves(the_line, tmp_path):
    assert _answer(the_line("join", "7", HEAD, "1")) == ""
    the_line("join", "9", MOVED, "2")
    assert _waiting(the_line) == [f"7 {HEAD} 1", f"9 {MOVED} 2"]
    the_line("leave", "7")
    the_line("leave", "7")  # leaving twice is no error
    assert _waiting(the_line) == [f"9 {MOVED} 2"]
    # Nothing but the places is left behind.
    assert os.listdir(tmp_path / "line") == ["9"]


def test_the_first_join_takes_its_ticket_from_the_clock_and_a_rejoin_keeps_it(
    the_line, tmp_path
):
    ticket = the_line("join", "7", HEAD, "").split()[0]
    assert int(ticket) > 0
    assert (tmp_path / "line" / "7").read_text().split() == [HEAD, f"@{ticket}"]
    the_line("leave", "7")
    assert the_line("join", "7", line.NO_HEAD, ticket).split() == [ticket]


def test_a_stand_reads_every_other_fresh_waiting_place_with_its_ticket(
    the_line, tmp_path
):
    the_line("join", "7", HEAD, "5")
    the_line("join", "9", MOVED, "3")
    the_line("join", "8", line.NO_HEAD, "4")
    the_line("join", "6", HEAD, "2")
    the_line("answer", "6", HEAD, line.MERGED)
    # A place a landing on older code wrote: a head and no ticket.
    (tmp_path / "line" / "10").write_text(f"{HEAD}\n")
    # A place whose landing stopped refreshing it more than `FRESH_S` ago.
    the_line("join", "11", HEAD, "1")
    os.utime(tmp_path / "line" / "11", (0, 0))
    reply = the_line("stand", "7", HEAD, "5").splitlines()
    assert reply[0] == "5"
    assert sorted(reply[1:]) == ["10 0", "8 4", "9 3"]
    # A holder stacks the drafts alone, and reads the older landing's place as the oldest.
    assert _waiting(the_line) == [f"10 {HEAD} 0", f"7 {HEAD} 5", f"9 {MOVED} 3"]


def test_an_answer_keeps_a_places_ticket_and_an_older_landings_format(
    the_line, tmp_path
):
    the_line("join", "7", HEAD, "5")
    the_line("answer", "7", HEAD, line.SPLIT)
    assert (tmp_path / "line" / "7").read_text().split() == [HEAD, "@5", line.SPLIT]
    (tmp_path / "line" / "8").write_text(f"{HEAD}\n")
    the_line("answer", "8", HEAD, line.MERGED)
    assert (tmp_path / "line" / "8").read_text().split() == [HEAD, line.MERGED]


def test_an_answer_reaches_the_landing_that_stands_with_that_head(the_line):
    the_line("join", "7", HEAD, "1")
    the_line("answer", "7", HEAD, line.MERGED)
    assert _answer(the_line("stand", "7", HEAD, "1")) == line.MERGED
    assert _waiting(the_line) == []
    assert _answer(the_line("stand", "7", MOVED, "1")) == ""
    assert _waiting(the_line) == [f"7 {MOVED} 1"]


def test_a_second_answer_leaves_the_first_in_place(the_line):
    the_line("join", "7", HEAD, "1")
    the_line("answer", "7", HEAD, line.MERGED)
    the_line("answer", "7", HEAD, line.ALONE)
    assert _answer(the_line("stand", "7", HEAD, "1")) == line.MERGED


@pytest.mark.parametrize(
    ("then", "waiting"),
    [(line.ALONE, []), ("", [f"7 {HEAD} 1"])],
    ids=["alone", "no-answer"],
)
def test_a_split_place_takes_the_holders_final_answer(the_line, then, waiting):
    the_line("join", "7", HEAD, "1")
    the_line("answer", "7", HEAD, line.SPLIT)
    # A split place is not waiting, so no other holder stacks it.
    assert _waiting(the_line) == []
    the_line("answer", "7", HEAD, then)
    assert _answer(the_line("stand", "7", HEAD, "1")) == then
    assert _waiting(the_line) == waiting


def test_a_landing_that_joins_never_reads_an_answer_left_for_an_earlier_one(the_line):
    the_line("join", "7", HEAD, "1")
    the_line("answer", "7", HEAD, line.MERGED)
    assert _answer(the_line("join", "7", HEAD, "1")) == ""
    assert _answer(the_line("stand", "7", HEAD, "1")) == ""


@pytest.mark.parametrize("pr", ["7", "8"], ids=["another-head", "nobody-standing"])
def test_an_answer_for_a_head_nobody_stands_with_is_dropped(the_line, tmp_path, pr):
    the_line("join", "7", MOVED, "1")
    the_line("answer", pr, HEAD, line.MERGED)
    # The line is untouched: one place, still waiting with its own head and unanswered.
    assert os.listdir(tmp_path / "line") == ["7"]
    assert not (tmp_path / "line" / "8").is_symlink()


@pytest.mark.parametrize("pr", ["../turn.lock", "7/8", "", "."])
def test_a_place_is_named_by_a_number_and_nothing_else(the_line, tmp_path, pr):
    (tmp_path / "turn.lock").touch()
    with pytest.raises(ValueError, match="not a pull request number"):
        the_line("leave", pr)
    assert (tmp_path / "turn.lock").exists()


def test_the_line_is_a_sticky_directory_every_user_can_write_whatever_the_umask(
    the_line, tmp_path
):
    old = os.umask(0o077)
    try:
        the_line("join", "7", HEAD, "1")
    finally:
        os.umask(old)
    assert (tmp_path / "line").stat().st_mode & 0o7777 == 0o1777


def test_an_operation_the_line_does_not_have_is_refused(the_line):
    with pytest.raises(ValueError, match="no such operation on the line: jump"):
        the_line("jump")


def test_a_landing_on_another_host_stands_in_the_same_line_over_ssh(
    the_line, tmp_path, ssh_here
):
    remote = line.over_ssh("turn-host", tmp_path / "line")
    the_line("join", "9", MOVED, "1")
    assert remote("join", "7", HEAD, "2").splitlines() == ["2", "9 1"]
    assert _waiting(the_line) == _waiting(remote) == [f"7 {HEAD} 2", f"9 {MOVED} 1"]
    the_line("answer", "7", HEAD, line.MERGED)
    assert _answer(remote("stand", "7", HEAD, "2")) == line.MERGED
    remote("leave", "7")
    assert not (tmp_path / "line" / "7").exists()


def test_a_state_record_replaces_this_users_last_one_and_takes_no_place(
    the_line, tmp_path, ssh_here
):
    the_line("join", "7", HEAD, "1")
    # The name another user's landing of the same PR writes, which the sticky bit would keep
    # this user from replacing.
    theirs = tmp_path / "line" / f"7.{os.getuid() + 1}.state"
    theirs.write_text('{"state": "verdict"}')
    line.over_ssh("turn-host", tmp_path / "line")("record", "7", '{"state": "queued"}')
    the_line("record", "7", '{"state": "holding"}')
    mine = tmp_path / "line" / f"7.{os.getuid()}.state"
    assert mine.read_text() == '{"state": "holding"}'
    assert theirs.read_text() == '{"state": "verdict"}'
    assert _waiting(the_line) == [f"7 {HEAD} 1"]
    assert sorted(os.listdir(tmp_path / "line")) == sorted(
        ["7", mine.name, theirs.name]
    )


def test_a_turn_host_that_does_not_answer_the_line_raises_oserror(
    tmp_path, monkeypatch
):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'Warning: added a key' >&2\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    with pytest.raises(OSError, match="ssh turn-host: ssh: no route"):
        line.over_ssh("turn-host", tmp_path / "line")("join", "7", HEAD, "1")
