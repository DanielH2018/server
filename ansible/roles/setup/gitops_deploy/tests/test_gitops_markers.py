"""The shared marker parsers: one good line and the garbage each must read as nothing.

There is one parser per format, so what to pin is each parser's own must-fire / must-not-fire pair: a
reader that guessed at a torn line would page on a lock or a role nobody can find.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_markers.py
"""

import pytest

from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    ContentionEntry,
    owed_clear_cmd,
    parse_behind,
    parse_contention,
)

_SHA = "abc1230000000000000000000000000000000000"


# ── behind_since ──────────────────────────────────────────────────────────────────────────
def test_a_behind_line_yields_the_sha_and_the_stamp():
    assert parse_behind(f"{_SHA} 1000") == (_SHA, 1000.0)


@pytest.mark.parametrize("text", [None, "", _SHA, f"{_SHA} not-a-stamp", "a b c"])
def test_a_garbled_behind_marker_reads_as_not_behind(text):
    assert parse_behind(text) is None


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
    assert owed_clear_cmd("k8s_deferred", "sonarr").endswith(
        "gitops_state.py clear-owed k8s_deferred sonarr"
    )
    assert CONTENTION_CLEAR_CMD.endswith("clear-contention")


def test_an_owed_clear_names_applied_tags_only_where_the_cli_takes_them():
    """`gitops_state.py` refuses `--applied` off `manual_plane`, so it must never be printed."""
    assert owed_clear_cmd("manual_plane", "k3s", {"k3s", "kubeconfig"}).endswith(
        "clear-owed manual_plane k3s --applied kubeconfig"
    )
    with pytest.raises(ValueError, match="manual_plane only"):
        owed_clear_cmd("k8s_unapplied", "authelia", {"authelia"})
