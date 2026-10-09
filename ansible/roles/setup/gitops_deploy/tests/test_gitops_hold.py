"""The hold's one owner: which apply clears a plane, and what `Hold` does with the files.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_hold.py
"""

import contextlib
import pathlib

import pytest

from gitops_hold import (
    read_field,
    DeployerSnapshot,
    Hold,
    broad_hold_cleared_by,
    held_tag,
    hold_plane_marker,
)
from gitops_ledger import OWED_HOLD_PLANE, owed_line
from gitops_markers import MARKERS

SHA = "a" * 40


# ── which apply clears a broad hold ───────────────────────────────────────────────────────────
# A hold names one unapplied plane, and every consumer gates on hold_sha, so clearing it after
# a success in another plane reads as "the pipeline is fine" over a plane nothing applied.
def test_the_same_playbook_untagged_clears_a_deploy_plane_hold():
    assert broad_hold_cleared_by("ansible/deploy.yml", "ansible/deploy.yml", [])


def test_another_playbook_does_not_clear_it():
    assert not broad_hold_cleared_by(
        "ansible/deploy.yml", "ansible/initial_setup.yml", ["gitops_deploy"]
    )


def test_an_untagged_run_covers_any_tag_set_held_against_it():
    assert broad_hold_cleared_by(
        "ansible/initial_setup.yml k3s", "ansible/initial_setup.yml", []
    )


def test_a_superset_of_tags_clears_it():
    assert broad_hold_cleared_by(
        "ansible/initial_setup.yml k3s", "ansible/initial_setup.yml", ["k3s", "dns"]
    )


def test_a_subset_of_tags_does_not():
    assert not broad_hold_cleared_by(
        "ansible/initial_setup.yml k3s,dns", "ansible/initial_setup.yml", ["k3s"]
    )


# A narrowed setup apply holds `<role>:<block>` (#3138): the block or its whole role covers it.
NARROWED_HOLD = "ansible/initial_setup.yml gitops_deploy:gitops-config"


def test_the_block_it_ran_clears_a_narrowed_hold():
    assert broad_hold_cleared_by(
        NARROWED_HOLD, "ansible/initial_setup.yml", ["gitops-config"]
    )


def test_the_whole_role_clears_a_narrowed_hold():
    assert broad_hold_cleared_by(
        NARROWED_HOLD, "ansible/initial_setup.yml", ["gitops_deploy"]
    )


def test_another_block_of_the_same_role_does_not_clear_a_narrowed_hold():
    assert not broad_hold_cleared_by(
        NARROWED_HOLD, "ansible/initial_setup.yml", ["gitops-timer"]
    )


def test_another_roles_tag_does_not_clear_a_narrowed_hold():
    assert not broad_hold_cleared_by(
        NARROWED_HOLD, "ansible/initial_setup.yml", ["renovate_notify"]
    )


def test_a_role_tag_holds_bare_so_a_hold_from_before_the_qualifier_reads_the_same():
    assert held_tag("gitops_deploy", "gitops_deploy") == "gitops_deploy"
    assert held_tag("gitops_deploy", "gitops-config") == "gitops_deploy:gitops-config"
    assert broad_hold_cleared_by(
        "ansible/initial_setup.yml gitops_deploy",
        "ansible/initial_setup.yml",
        ["gitops_deploy"],
    )


def test_a_tagged_run_does_not_clear_an_untagged_hold():
    assert not broad_hold_cleared_by(
        "ansible/initial_setup.yml", "ansible/initial_setup.yml", ["k3s"]
    )


def test_no_hold_is_nothing_to_keep():
    assert broad_hold_cleared_by("", "ansible/deploy.yml", [])


def test_the_marker_format_round_trips():
    marker = hold_plane_marker("ansible/initial_setup.yml", ["k3s", "dns"])
    assert marker == "ansible/initial_setup.yml k3s,dns"
    assert broad_hold_cleared_by(marker, "ansible/initial_setup.yml", ["k3s", "dns"])
    assert hold_plane_marker("ansible/deploy.yml", []) == "ansible/deploy.yml"


# ── Hold: the two files behind one object ─────────────────────────────────────────────────


@pytest.fixture
def hold(tmp_path: pathlib.Path) -> Hold:
    return Hold(tmp_path)


def _hold_sha(hold: Hold) -> pathlib.Path:
    return hold.state_dir / MARKERS["hold"]


def test_cover_keeps_hold_sha_while_another_plane_is_unapplied(hold: Hold):
    hold.record(SHA, "ansible/deploy.yml", ["sonarr"])
    hold.record(SHA, "ansible/initial_setup.yml", ["k3s"])

    assert hold.cover("ansible/deploy.yml", ["sonarr"]) == [
        "ansible/initial_setup.yml k3s"
    ]
    assert hold.current() == SHA
    assert hold.cover("ansible/initial_setup.yml", ["k3s"]) == []
    assert hold.current() is None


def test_cover_services_with_no_service_covers_no_plane(hold: Hold):
    """`cover` reads an empty tag list as the whole playbook; a deploy of nothing is not that."""
    hold.record(SHA, "ansible/deploy.yml", ["sonarr"])

    assert hold.cover_services(set()) == ["ansible/deploy.yml sonarr"]
    assert hold.current() == SHA


def test_clear_with_no_plane_held_still_takes_the_lock(hold: Hold):
    """A tick can write a plane between the check and the unlink, so the lock is always taken."""
    _hold_sha(hold).write_text(SHA)

    def refuse() -> contextlib.AbstractContextManager[None]:
        raise TimeoutError("tree.lock")

    with pytest.raises(TimeoutError):
        hold.clear(SHA, lock=refuse)
    assert hold.current() == SHA


@pytest.mark.parametrize("planes", [[], ["sonarr"]], ids=["no-plane", "one-plane"])
def test_clear_refuses_a_hold_a_tick_rewrote_before_the_lock(hold: Hold, planes):
    """A tick that fails between the operator's check and the lock writes a hold they never typed (#3755)."""
    other = "b" * 40
    _hold_sha(hold).write_text(SHA)
    for service in planes:
        hold.record(SHA, "ansible/deploy.yml", [service])

    @contextlib.contextmanager
    def tick_fails_first():
        hold.record(other, "ansible/initial_setup.yml", ["k3s"])
        yield

    refusal = hold.clear(SHA, lock=tick_fails_first)

    assert refusal is not None and "reload and retry" in refusal
    assert hold.current() == other
    assert "ansible/initial_setup.yml k3s" in hold.held_subjects()


def test_clear_drops_a_torn_plane_line_too(hold: Hold):
    """A torn `hold_plane` line still names a plane; leaving it would hold over a clear SHA."""
    _hold_sha(hold).write_text(SHA)
    whole = owed_line(OWED_HOLD_PLANE, "ansible/deploy.yml", SHA, 1)
    torn = '{"class": "hold_plane", "subject": "ansible/initial_setup.yml k3s"}'
    (hold.state_dir / MARKERS["owed"]).write_text(f"{whole}\n{torn}")

    assert hold.clear(SHA) is None
    assert hold.held_subjects() == []
    assert hold.current() is None


# ── DeployerSnapshot, the one read view for readers outside the deployer (#3703) ──────────────
def test_a_snapshot_of_an_empty_state_dir_reads_every_marker_absent(tmp_path):
    (tmp_path / MARKERS["hold"]).write_text("\n")
    assert DeployerSnapshot.load(tmp_path) == DeployerSnapshot(
        None, None, None, None, None
    )


def test_a_snapshot_reads_each_marker_and_the_planes_a_hold_waits_on(tmp_path):
    (tmp_path / MARKERS["hold"]).write_text(SHA + "\n")
    (tmp_path / MARKERS["behind"]).write_text("bbb 100\n")
    (tmp_path / MARKERS["contention"]).write_text("ccc lock 1 2 3\n")
    (tmp_path / MARKERS["diverged"]).write_text("ddd\n")
    torn = b'{"class": "hold_plane", "subject": "torn\xff", "origin": "a", "at": 2}'
    line = owed_line(OWED_HOLD_PLANE, "ansible/deploy.yml sonarr", SHA, 1.0)
    (tmp_path / MARKERS["owed"]).write_bytes(line.encode() + b"\n" + torn)
    snap = DeployerSnapshot.load(tmp_path)
    assert (snap.hold, snap.behind, snap.contention, snap.diverged) == (
        SHA,
        "bbb 100",
        "ccc lock 1 2 3",
        "ddd",
    )
    assert snap.owed == line
    assert snap.held_planes == ["ansible/deploy.yml sonarr"]


def test_a_snapshot_raises_on_a_marker_it_cannot_read(tmp_path):
    """An unreadable `hold_sha` is not "no hold": the caller decides what it means."""
    (tmp_path / MARKERS["hold"]).mkdir()
    with pytest.raises(IsADirectoryError):
        DeployerSnapshot.load(tmp_path)


def test_read_field_reads_one_marker_past_a_torn_sibling(tmp_path):
    """`load` raises on the torn `behind_since`; `read_field` for `hold` does not read it."""
    (tmp_path / MARKERS["hold"]).write_text(SHA + "\n")
    (tmp_path / MARKERS["behind"]).write_bytes(b"\xff\n")
    assert read_field(tmp_path, "hold") == SHA
    with pytest.raises(UnicodeDecodeError):
        read_field(tmp_path, "behind")
    with pytest.raises(UnicodeDecodeError):
        DeployerSnapshot.load(tmp_path)
