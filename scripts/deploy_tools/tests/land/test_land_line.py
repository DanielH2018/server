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


def test_a_landing_that_joins_is_in_the_line_until_it_leaves(the_line, tmp_path):
    assert the_line("join", "7", HEAD) == ""
    the_line("join", "9", MOVED)
    assert _waiting(the_line) == [f"7 {HEAD}", f"9 {MOVED}"]
    the_line("leave", "7")
    the_line("leave", "7")  # leaving twice is no error
    assert _waiting(the_line) == [f"9 {MOVED}"]
    # Nothing but the places is left behind.
    assert os.listdir(tmp_path / "line") == ["9"]


def test_an_answer_reaches_the_landing_that_stands_with_that_head(the_line):
    the_line("join", "7", HEAD)
    the_line("answer", "7", HEAD, line.MERGED)
    assert the_line("stand", "7", HEAD) == line.MERGED
    assert _waiting(the_line) == []
    assert the_line("stand", "7", MOVED) == ""
    assert _waiting(the_line) == [f"7 {MOVED}"]


def test_a_second_answer_leaves_the_first_in_place(the_line):
    the_line("join", "7", HEAD)
    the_line("answer", "7", HEAD, line.MERGED)
    the_line("answer", "7", HEAD, line.ALONE)
    assert the_line("stand", "7", HEAD) == line.MERGED


@pytest.mark.parametrize(
    ("then", "waiting"),
    [(line.ALONE, []), ("", [f"7 {HEAD}"])],
    ids=["alone", "no-answer"],
)
def test_a_split_place_takes_the_holders_final_answer(the_line, then, waiting):
    the_line("join", "7", HEAD)
    the_line("answer", "7", HEAD, line.SPLIT)
    # A split place is not waiting, so no other holder stacks it.
    assert _waiting(the_line) == []
    the_line("answer", "7", HEAD, then)
    assert the_line("stand", "7", HEAD) == then
    assert _waiting(the_line) == waiting


def test_a_landing_that_joins_never_reads_an_answer_left_for_an_earlier_one(the_line):
    the_line("join", "7", HEAD)
    the_line("answer", "7", HEAD, line.MERGED)
    assert the_line("join", "7", HEAD) == ""
    assert the_line("stand", "7", HEAD) == ""


@pytest.mark.parametrize("pr", ["7", "8"], ids=["another-head", "nobody-standing"])
def test_an_answer_for_a_head_nobody_stands_with_is_dropped(the_line, tmp_path, pr):
    the_line("join", "7", MOVED)
    the_line("answer", pr, HEAD, line.MERGED)
    # The line is untouched: one place, still waiting with its own head and unanswered.
    assert os.listdir(tmp_path / "line") == ["7"]
    assert _waiting(the_line) == [f"7 {MOVED}"]
    assert the_line("stand", "7", MOVED) == ""


def test_a_place_no_landing_refreshes_is_removed_until_one_stands_again(
    the_line, tmp_path
):
    the_line("join", "7", HEAD)
    place = tmp_path / "line" / "7"
    stale = place.stat().st_mtime - line.FRESH_S - 5
    os.utime(place, (stale, stale))
    assert _waiting(the_line) == []
    assert not place.exists()
    the_line("stand", "7", HEAD)
    assert _waiting(the_line) == [f"7 {HEAD}"]


def test_standing_keeps_a_place_fresh(the_line, tmp_path):
    the_line("join", "7", HEAD)
    place = tmp_path / "line" / "7"
    stale = place.stat().st_mtime - line.FRESH_S - 5
    os.utime(place, (stale, stale))
    the_line("stand", "7", HEAD)
    assert _waiting(the_line) == [f"7 {HEAD}"]


def test_a_landing_reads_only_the_places_of_its_own_user(
    the_line, tmp_path, monkeypatch
):
    the_line("join", "7", HEAD)
    the_line("join", "9", HEAD)
    the_line("answer", "9", HEAD, line.MERGED)
    someone_else = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: someone_else)
    place = tmp_path / "line" / "7"
    stale = place.stat().st_mtime - line.FRESH_S - 5
    os.utime(place, (stale, stale))
    # Another user's places are neither listed nor removed, stale or not.
    assert _waiting(the_line) == []
    assert place.exists()
    # An answer in a place another user wrote is not this landing's answer.
    assert the_line("stand", "9", HEAD) == ""


def test_the_listing_passes_over_what_is_not_a_place(the_line, tmp_path):
    the_line("join", "7", HEAD)
    (tmp_path / "line" / "5").mkdir()
    (tmp_path / "line" / "notes").write_text(f"{HEAD}\n")
    (tmp_path / "line" / "6").symlink_to(tmp_path / "gone")
    assert _waiting(the_line) == [f"7 {HEAD}"]


def test_a_link_left_at_a_places_name_is_replaced_and_not_followed(the_line, tmp_path):
    the_line("join", "7", HEAD)
    target = tmp_path / "target"
    target.write_text("keep\n")
    (tmp_path / "line" / "8").symlink_to(target)
    the_line("join", "8", HEAD)
    assert target.read_text() == "keep\n"
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
        the_line("join", "7", HEAD)
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
    assert remote("join", "7", HEAD) == ""
    assert _waiting(the_line) == _waiting(remote) == [f"7 {HEAD}"]
    the_line("answer", "7", HEAD, line.MERGED)
    assert remote("stand", "7", HEAD) == line.MERGED
    remote("leave", "7")
    assert not (tmp_path / "line" / "7").exists()


def test_a_state_record_replaces_the_last_one_and_takes_no_place(
    the_line, tmp_path, ssh_here
):
    the_line("join", "7", HEAD)
    line.over_ssh("turn-host", tmp_path / "line")("record", "7", '{"state": "queued"}')
    the_line("record", "7", '{"state": "holding"}')
    assert (tmp_path / "line" / "7.state").read_text() == '{"state": "holding"}'
    assert _waiting(the_line) == [f"7 {HEAD}"]
    assert sorted(os.listdir(tmp_path / "line")) == ["7", "7.state"]


def test_a_turn_host_that_does_not_answer_the_line_raises_oserror(
    tmp_path, monkeypatch
):
    stubs = fake_bin(
        tmp_path / "bin",
        ssh="#!/usr/bin/env bash\necho 'Warning: added a key' >&2\necho 'ssh: no route' >&2\nexit 255\n",
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    with pytest.raises(OSError, match="ssh turn-host: ssh: no route"):
        line.over_ssh("turn-host", tmp_path / "line")("join", "7", HEAD)
