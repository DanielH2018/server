"""The shared marker parsers: one good line and the garbage each must read as nothing.

These took over from `ansible/tests/deploy/test_{contention,manual_plane}_parsers_agree.py`,
which fed three independent parsers the same lines (issue #2063). There is one parser per
format now, so what remains to pin is each parser's own must-fire / must-not-fire pair: a
reader that guessed at a torn line would page on a lock or a role nobody can find.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_markers.py
"""

import pytest

from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    MANUAL_PLANE_CLEAR_CMD,
    ContentionEntry,
    ManualPlaneEntry,
    parse_behind,
    parse_contention,
    parse_manual_plane,
)

_SHA = "abc1230000000000000000000000000000000000"


# ── behind_since ──────────────────────────────────────────────────────────────────────────
def test_a_behind_line_yields_the_sha_and_the_stamp():
    assert parse_behind(f"{_SHA} 1000") == (_SHA, 1000.0)


@pytest.mark.parametrize("text", [None, "", _SHA, f"{_SHA} not-a-stamp", "a b c"])
def test_a_garbled_behind_marker_reads_as_not_behind(text):
    assert parse_behind(text) is None


# ── manual_plane ──────────────────────────────────────────────────────────────────────────
_MANUAL = "\n".join(
    [
        f"{_SHA} ansible/k3s-bringup.yml k3s 1000",
        "three fields only",
        "beef1230000000000000000000000000000000000 none common 2000",
        "cafe1230000000000000000000000000000000000 none broken not-a-stamp",
    ]
)


def test_every_parseable_manual_plane_line_is_an_entry_in_file_order():
    assert parse_manual_plane(_MANUAL) == [
        ManualPlaneEntry(_SHA, "ansible/k3s-bringup.yml", "k3s", 1000.0),
        ManualPlaneEntry(
            "beef1230000000000000000000000000000000000", "none", "common", 2000.0
        ),
    ]


def test_a_garbled_manual_plane_line_is_skipped_and_its_neighbours_survive():
    roles = [e.role for e in parse_manual_plane(_MANUAL)]
    assert "broken" not in roles and roles == ["k3s", "common"]


@pytest.mark.parametrize("text", [None, "", "\n\n"])
def test_an_empty_manual_plane_marker_has_nothing_pending(text):
    assert parse_manual_plane(text) == []


# ── contention_since ──────────────────────────────────────────────────────────────────────
def test_a_contention_line_yields_the_whole_streak():
    assert parse_contention(f"{_SHA} sonarr 1000.0 1900.0 2") == ContentionEntry(
        _SHA, "sonarr", 1000.0, 1900.0, 2
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        f"{_SHA} sonarr not-a-stamp 1900.0 2",
        "four fields only here",
        "a b c d e f",
    ],
)
def test_a_garbled_contention_marker_reads_as_no_streak(text):
    assert parse_contention(text) is None


# ── the clear commands every surface prints ───────────────────────────────────────────────
def test_the_clear_commands_name_the_gitops_state_subcommands():
    assert "clear-manual-plane <role>" in MANUAL_PLANE_CLEAR_CMD
    assert CONTENTION_CLEAR_CMD.endswith("clear-contention")
