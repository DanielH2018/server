"""Tests for the shared parked-deployer decision.

Two readers ask it — the SessionStart banner and `deploy.sh` exit 4 — so every rule here is a
must-fire / must-not-fire pair: a decision that answered "parked" for everything and one that
answered it for nothing are indistinguishable from the passing side alone.

Run: uv run pytest scripts/lib/tests/test_deployer_park.py
"""

import json

import pytest

from lib.deployer_park import (
    BEHIND_PARK_SECONDS,
    manual_plane_lines,
    park_age,
    park_note,
    read_behind_marker,
    read_manual_plane_marker,
)
from gitops_markers import MARKERS
import gitops_markers


@pytest.fixture(autouse=True)
def _agent_tables_hold_k3s(monkeypatch):
    """The printers' tables are empty since H7.34, and H7.53 deletes the printers with the deployer."""
    monkeypatch.setitem(gitops_markers.AGENT_JOIN_TAGS, "k3s", "k3s_agent")
    monkeypatch.setitem(gitops_markers.AGENT_PIN_TAGS, "k3s", "node-address")


# The shape the deployer writes: the origin SHA it is behind, then when it first saw it.
_MARKER = "abc1230000000000000000000000000000000000 1000"


def test_a_stamp_older_than_the_threshold_is_a_park():
    assert park_age(_MARKER, 1000 + BEHIND_PARK_SECONDS + 60) is not None


def test_a_stamp_within_the_threshold_is_a_queue():
    assert park_age(_MARKER, 1000 + BEHIND_PARK_SECONDS - 60) is None


def test_the_age_is_measured_from_the_stamp_not_from_now():
    """The banner prints minutes, so the number has to be the age of the marker."""
    assert park_age(_MARKER, 1000 + 3600) == 3600


def test_an_absent_marker_is_not_a_park():
    assert park_age(None, 1e9) is None
    assert park_age("", 1e9) is None


def test_a_torn_marker_is_not_a_park():
    """Written atomically, so a value that will not parse is damage, never an old park."""
    assert park_age("not-a-timestamp", 1e9) is None


def test_the_note_names_the_primary_checkout_when_parked():
    note = park_note(_MARKER, 1000 + 3600)
    assert "primary checkout" in note
    assert "60 min" in note
    # The unit's ExecStart is flock, so its lines carry SYSLOG_IDENTIFIER=flock and
    # `journalctl -t gitops-deploy` prints `-- No entries --` (#6343).
    assert "journalctl -u gitops-deploy.service" in note
    assert "journalctl -t gitops-deploy" not in note


def test_the_note_is_empty_when_the_deployer_is_merely_behind():
    assert park_note(_MARKER, 1000 + BEHIND_PARK_SECONDS - 60) == ""


def test_the_marker_is_read_from_the_state_directory(tmp_path):
    (tmp_path / MARKERS["behind"]).write_text(_MARKER + "\n")
    assert read_behind_marker(str(tmp_path)) == _MARKER


def test_a_missing_state_directory_reads_as_no_marker(tmp_path):
    """Every host but daniel-box has none, and that must not be an error."""
    assert read_behind_marker(str(tmp_path / "nope")) is None


def test_a_torn_byte_in_the_marker_reads_as_no_park(tmp_path):
    """A byte that does not decode is "absent" here, never a raise that ends the banner."""
    (tmp_path / MARKERS["behind"]).write_bytes(b"abc \xff 1000\n")
    assert read_behind_marker(str(tmp_path)) is None


def test_a_torn_sibling_marker_does_not_hide_the_park(tmp_path):
    """Each marker is read alone, so a torn `hold_sha` leaves the park visible."""
    (tmp_path / MARKERS["behind"]).write_text(_MARKER + "\n")
    (tmp_path / MARKERS["hold"]).write_bytes(b"\xff\n")
    assert read_behind_marker(str(tmp_path)) == _MARKER


# ── the owed ledger's manual_plane class; its parser is tested with `gitops_ledger` ──────


def test_a_manual_plane_ledger_line_with_an_unknown_key_reaches_the_banner(tmp_path):
    """The Verify-by of #3392 for the banner: a key a newer writer added is ignored."""
    line = {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": "f" * 40,
        "at": 500,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": ["kubeconfig"],
        "added_by_a_newer_writer": 1,
    }
    (tmp_path / MARKERS["owed"]).write_text(json.dumps(line) + "\n")
    (line_text,) = manual_plane_lines(read_manual_plane_marker(str(tmp_path)), 4000)
    assert "`k3s` setup role 58 min ago" in line_text
    assert "--tags kubeconfig" in line_text
    assert line_text.endswith("clear-owed manual_plane k3s --applied kubeconfig`")


def test_a_pi_only_role_is_applied_with_its_target(tmp_path):
    """#3933: run on daniel-box, `--tags optimize_pi` skips the role and exits 0.

    The target is the line's own `host` key, which the deployer derives when it records it.
    """
    line = {
        "class": "manual_plane",
        "subject": "optimize_pi",
        "origin": "f" * 40,
        "at": 500,
        "playbook": "ansible/initial_setup.yml",
        "host": "daniel-pi",
    }
    (tmp_path / MARKERS["owed"]).write_text(json.dumps(line) + "\n")
    (line_text,) = manual_plane_lines(read_manual_plane_marker(str(tmp_path)), 4000)
    assert (
        "apply `uv run ansible-playbook ansible/initial_setup.yml --tags optimize_pi "
        "-e target=daniel-pi` by hand" in line_text
    )


def test_an_absent_ledger_reads_as_nothing_pending(tmp_path):
    assert read_manual_plane_marker(str(tmp_path / "nope")) is None
    assert manual_plane_lines(None, 4000) == []


def test_the_playbook_prefix_is_defined_only_in_gitops_markers():
    """`deployer_park` prints the prefix the deployer and monitor-bridge print, not its own (#4413)."""
    import re
    import subprocess

    from lib.repo_paths import REPO

    listed = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    assert "scripts/lib/deployer_park.py" in listed
    assignment = re.compile(r"^PLAYBOOK_CMD\s*=", re.MULTILINE)
    defining = {rel for rel in listed if assignment.search((REPO / rel).read_text())}
    assert defining == {
        "ansible/roles/setup/gitops_deploy/files/gitops_markers.py",
        "ansible/roles/k8s/monitor-bridge/files/gitops_markers.py",
    }


def _in_order(text: str, *parts: str) -> None:
    """Each of `parts` appears in `text`, first occurrences in the order given."""
    at = [text.find(p) for p in parts]
    assert -1 not in at, (
        f"missing {[p for p, i in zip(parts, at, strict=True) if i < 0]} in {text}"
    )
    assert at == sorted(at), f"{parts} out of order in {text}"


def test_a_whole_role_k3s_line_names_the_agent_join_before_the_clear(tmp_path):
    """#5627: `--tags k3s` on daniel-box never reaches an agent node (#5606).

    The deployer carries the agent hosts on the line, as it carries `host`, because this
    banner cannot import the routing that names them.
    """
    line = {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": "f" * 40,
        "at": 500,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": [],
        "agent_hosts": ["daniel-server"],
    }
    (tmp_path / MARKERS["owed"]).write_text(json.dumps(line) + "\n")
    (line_text,) = manual_plane_lines(read_manual_plane_marker(str(tmp_path)), 4000)
    _in_order(
        line_text,
        "ansible/k3s-bringup.yml --tags k3s",
        "ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent",
        "ansible/k3s-bringup.yml -e pin_agent_address=daniel-server --tags node-address",
        "clear-owed manual_plane k3s",
    )


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
def test_a_narrowed_k3s_line_puts_its_agent_tag_on_the_join(
    tmp_path, tags, parts, absent
):
    line = {
        "class": "manual_plane",
        "subject": "k3s",
        "origin": "f" * 40,
        "at": 500,
        "playbook": "ansible/k3s-bringup.yml",
        "tags": tags,
        "agent_hosts": ["daniel-server"],
    }
    (tmp_path / MARKERS["owed"]).write_text(json.dumps(line) + "\n")
    (line_text,) = manual_plane_lines(read_manual_plane_marker(str(tmp_path)), 4000)
    _in_order(line_text, *parts)
    assert absent not in line_text, line_text
    assert "``" not in line_text, line_text
