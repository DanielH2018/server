"""Tests for `probe.py gitops-state`, the read-only view of the deployer's markers (#3931).

Each test builds a state directory in `tmp_path` and hands it to `collect`, so none reads the
host's `/var/lib/gitops-deploy`.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import gitops_view as view
from lib.exit_codes import PROBE_UNREADABLE

SHA = "2d25ced3" + "0" * 32
NOW = 1_800_000_000.0


def _owed(cls: str, subject: str, at: float = NOW - 3600, **extra) -> str:
    return json.dumps(
        {**extra, "class": cls, "subject": subject, "origin": SHA, "at": int(at)}
    )


def _state(tmp_path, **markers: str):
    for name, text in markers.items():
        (tmp_path / view.MARKERS[name]).write_text(text)
    return tmp_path


def test_every_set_marker_is_printed_with_its_way_out(tmp_path):
    """CLEAN half: the two #3931 names, contention and k8s_deferred, plus the hold's clear."""
    _state(
        tmp_path,
        last_run=f"{NOW - 30}\n",
        hold=SHA,
        contention=f"{SHA} sonarr {NOW - 7200} {NOW - 60} 12\n",
        owed="\n".join(
            [
                _owed("hold_plane", "ansible/deploy.yml sonarr"),
                _owed("k8s_deferred", "radarr"),
                _owed("k8s_unapplied", "homepage"),
            ]
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    assert "last_run:     2027-01-15 07:59:30 UTC (30s ago)" in out
    assert "plane: ansible/deploy.yml sonarr" in out
    assert f"{view.HOLD_CLEAR_CMD} {SHA}" in out
    assert "12 tick(s) deferred on service lock `sonarr`" in out
    assert view.CONTENTION_CLEAR_CMD in out
    assert "radarr  (origin 2d25ced3, 60 min ago)" in out
    assert "gitops_state.py clear-owed k8s_deferred radarr" in out
    assert "gitops_state.py clear-owed k8s_unapplied homepage" in out


def test_a_shared_role_unapplied_line_names_no_bare_fleet_redeploy(tmp_path):
    """#4373, the land.sh summary: `manifests` gets the render-proof wording, `homepage` its
    own deploy command."""
    _state(
        tmp_path,
        owed="\n".join(
            [_owed("k8s_unapplied", "manifests"), _owed("k8s_unapplied", "homepage")]
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    assert 'do:   ./scripts/deploy.sh --tags "homepage"' in out
    assert 'do:   ./scripts/deploy.sh --tags "manifests"' not in out
    assert "do:   nothing to deploy: `manifests` is a shared role" in out


def test_hold_plane_lines_with_no_hold_sha_are_shown_as_orphaned(tmp_path):
    """A hand `rm hold_sha` leaves planes the next hold waits on; `none` alone hid them."""
    _state(tmp_path, owed=_owed("hold_plane", "ansible/deploy.yml sonarr"))
    snap = view.collect(tmp_path, NOW)
    assert snap["hold"]["clear"] == f"{view.HOLD_CLEAR_CMD} --orphaned"
    out = view.format_text(snap)
    assert "hold_sha:     none, but 1 orphaned hold_plane line(s)" in out
    assert "plane: ansible/deploy.yml sonarr" in out


def test_absent_markers_read_as_none_and_exit_zero(tmp_path, capsys):
    """An empty state directory is a deployer with nothing to say, which reads cleanly."""
    assert view.run_gitops_state(SimpleNamespace(json=False), tmp_path, NOW) == 0
    out = capsys.readouterr().out
    for line in ("hold_sha:     none", "contention:   none", "k8s_deferred:  none"):
        assert line in out


def test_an_unreadable_marker_is_named_unreadable_not_none(tmp_path, capsys):
    """FLAGGED half: the `claude` user on daniel-box sees `hold_sha` exist and cannot open it.

    `deployer_park`'s readers fold that into absent, which here would print `none` over a
    live hold.
    """
    _state(tmp_path, hold=SHA)
    (tmp_path / "hold_sha").chmod(0o000)
    try:
        assert (
            view.run_gitops_state(SimpleNamespace(json=True), tmp_path, NOW)
            == PROBE_UNREADABLE
        )
    finally:
        (tmp_path / "hold_sha").chmod(0o600)
    snap = json.loads(capsys.readouterr().out)
    assert snap["unreadable"] == ["hold_sha"]
    assert snap["hold"] == {
        "status": "unreadable",
        "sha": None,
        "planes": [],
        "clear": None,
    }


def test_an_unreadable_marker_names_the_acl_not_a_sudo_the_agent_lacks(tmp_path):
    """The `claude` user has no sudo, so `sudo -u ubuntu` was a hint it could not follow (#4198)."""
    _state(tmp_path, hold=SHA)
    (tmp_path / "hold_sha").chmod(0o000)
    try:
        out = view.format_text(view.collect(tmp_path, NOW))
    finally:
        (tmp_path / "hold_sha").chmod(0o600)
    assert "sudo" not in out
    assert "getfacl /var/lib/gitops-deploy" in out and "--tags claude_code" in out


def test_no_state_directory_exits_unreadable_and_says_where_it_lives(tmp_path, capsys):
    """Off daniel-box there is no directory, which must not read as a clean deployer."""
    ns = SimpleNamespace(json=False)
    assert view.run_gitops_state(ns, tmp_path / "missing", NOW) == PROBE_UNREADABLE
    out = capsys.readouterr().out
    assert "no state directory here" in out
    assert "hold_sha" not in out


def test_reading_writes_nothing(tmp_path):
    """The view must not tick: the directory is byte-identical after a read."""
    _state(tmp_path, hold=SHA, owed=_owed("k8s_deferred", "radarr"))
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()
    }
    view.collect(tmp_path, NOW)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert after == before


def test_the_manual_plane_apply_names_the_pinned_playbook_command(tmp_path):
    """The `do:` line printed `<playbook> --tags <tags>`, naming no program (#4413)."""
    _state(
        tmp_path,
        owed=_owed(
            "manual_plane",
            "optimize_pi",
            playbook="ansible/initial_setup.yml",
            tags=[],
            host="daniel-pi",
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    assert (
        "do:   uv run ansible-playbook ansible/initial_setup.yml --tags optimize_pi"
        " -e target=daniel-pi" in out
    ), out


def test_a_whole_role_k3s_row_names_the_agent_join_before_the_clear(tmp_path):
    """#5627: `--tags k3s` on daniel-box never reaches an agent node (#5606)."""
    _state(
        tmp_path,
        owed=_owed(
            "manual_plane",
            "k3s",
            playbook="ansible/k3s-bringup.yml",
            tags=[],
            agent_hosts=["daniel-server"],
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    parts = (
        "ansible/k3s-bringup.yml --tags k3s",
        "ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent",
        "ansible/k3s-bringup.yml -e pin_agent_address=daniel-server --tags node-address",
        "clear-owed manual_plane k3s",
    )
    at = [out.find(p) for p in parts]
    assert -1 not in at, (
        f"missing {[p for p, i in zip(parts, at, strict=True) if i < 0]} in {out}"
    )
    assert at == sorted(at), f"{parts} out of order in {out}"


_JOIN = "ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent"
_PIN = "ansible/k3s-bringup.yml -e pin_agent_address=daniel-server --tags node-address"

# A narrowed k3s row splits: `k3s_agent` goes on the join command, the rest on the server
# command, and a row of `k3s_agent` alone prints no server command at all (#5628). A row of
# `node-address` keeps the server command and adds the pin play, run on the agent (#5918). The
# clear names the whole row either way.
_SPLIT_ROWS = [
    pytest.param(
        ["k3s_agent"],
        (_JOIN, "clear-owed manual_plane k3s --applied k3s_agent"),
        "k3s-bringup.yml --tags",
        id="agent-only",
    ),
    pytest.param(
        ["k3s_agent", "k3s_server"],
        (
            "ansible/k3s-bringup.yml --tags k3s_server",
            _JOIN,
            "clear-owed manual_plane k3s --applied k3s_agent,k3s_server",
        ),
        "--tags k3s_agent,",
        id="both-halves",
    ),
    pytest.param(
        ["node-address"],
        (
            "ansible/k3s-bringup.yml --tags node-address",
            _PIN,
            "(run on daniel-server)",
            "clear-owed manual_plane k3s --applied node-address",
        ),
        "join_agent",
        id="pin-play",
    ),
]


@pytest.mark.parametrize(("tags", "parts", "absent"), _SPLIT_ROWS)
def test_a_narrowed_k3s_row_puts_its_agent_tag_on_the_join(
    tmp_path, tags, parts, absent
):
    _state(
        tmp_path,
        owed=_owed(
            "manual_plane",
            "k3s",
            playbook="ansible/k3s-bringup.yml",
            tags=tags,
            agent_hosts=["daniel-server"],
        ),
    )
    out = view.format_text(view.collect(tmp_path, NOW))
    at = [out.find(p) for p in parts]
    assert -1 not in at, (
        f"missing {[p for p, i in zip(parts, at, strict=True) if i < 0]} in {out}"
    )
    assert at == sorted(at), f"{parts} out of order in {out}"
    assert absent not in out, out
