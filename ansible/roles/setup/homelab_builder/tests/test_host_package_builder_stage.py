"""The builder's `published/` record: a package counts as published only once the signer can take it.

2026-10-23: `shutil.copytree` raised on the Pi package's dangling `timers.target.wants` link
after it had copied every other file, `stage()` never reached `state["staged"] = ...`, and the
next run read the Pi package as already published and deleted its staged `.deb`. The render
here ships the same link. The cases are the link, the comparison that missed it, a run that
fails between `published/` and `state["staged"]`, the production leftover of that failure,
and a package the rollout gate defers out of an unsigned set.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder_stage.py
"""

import json
import os
from pathlib import Path

import pytest

import host_package_builder as hb
from test_host_package_builder import FPR, GREEN, FakeGitHub, Origin, git, metric
from test_host_package_builder import builder as make_builder
from test_host_package_builder_rollout import HostsBuilder, Prom, debs, edit, sign

PI = "hosts/daniel-pi"
LINK = Path("root/wants/x.timer")


@pytest.fixture
def lab(tmp_path):
    origin = Origin(tmp_path / "origin")
    state, etc = tmp_path / "state", tmp_path / "etc"
    (state / "staged").mkdir(parents=True)
    etc.mkdir()
    (etc / "archive-key.fpr").write_text(FPR + "\n")
    return origin, state, etc, FakeGitHub()


class LinkedBuilder(HostsBuilder):
    """The rollout fixture's render, with the Pi package carrying a link into a sibling package."""

    def render(self, tree: Path, out: Path) -> None:
        super().render(tree, out)
        if (out / PI).is_dir():
            (out / PI / LINK).parent.mkdir(parents=True)
            os.symlink("../x.timer", out / PI / LINK)


def go(lab, cls=LinkedBuilder, **overrides) -> int:
    """One run with every commit green; returns the exit code."""
    origin, _state, etc, github = lab
    for sha in git(origin.path, "rev-list", "master").split():
        github.runs[sha] = GREEN
    b = make_builder(lab, cls=cls, **overrides)
    b.release_path = etc / "release"
    b.prom_fetch = Prom()
    return b.main()


def recorded(state: Path) -> list[str]:
    return json.loads((state / "state.json").read_text()).get("staged", [])


def test_a_package_with_a_dangling_link_is_published_whole_and_staged(lab):
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    assert go(lab) == 0
    link = state / "published" / PI / LINK
    assert link.is_symlink() and os.readlink(link) == "../x.timer"
    assert "homelab-daniel-pi" in debs(state)
    assert metric(state)[0] == "ok"


def test_a_symlink_s_target_is_part_of_the_comparison(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    for tree in (left, right):
        tree.mkdir()
        os.symlink("../x.timer", tree / "link")
    assert hb.tree_files(left) == hb.tree_files(right)
    (right / "link").unlink()
    os.symlink("../y.timer", right / "link")
    assert hb.tree_files(left) != hb.tree_files(right), (
        "a retargeted link reads unchanged"
    )
    (right / "link").unlink()
    assert hb.tree_files(left) != hb.tree_files(right), "a removed link reads unchanged"


class FailsAfterPublish(LinkedBuilder):
    """A run that raises once `stage()` has copied the renders into published/."""

    def note_rollout(self, deferred, gate) -> None:
        raise OSError("the disk went away after the published/ copy")


def test_a_run_that_fails_after_publishing_leaves_the_package_to_stage_again(lab):
    origin, state, _, _ = lab
    edit(origin, "daniel-box", "box 1")
    assert go(lab) == 0
    sign(state)
    assert recorded(state) == ["hosts/daniel-box", "packages/homelab-updater"]

    edit(origin, "daniel-pi", "pi 1")
    assert go(lab, cls=FailsAfterPublish) == 1
    assert metric(state)[0] == "build"
    # The signer never ran: the failed run exited 1. Its deb sits in staged/ and is recorded.
    assert "homelab-daniel-pi" in debs(state) and PI in recorded(state)

    assert go(lab) == 0
    assert "homelab-daniel-pi" in debs(state), "the retry dropped the Pi's deb"


def test_the_leftover_of_a_followed_copy_stages_the_package_again(lab):
    # What production held after the failure above: published/ with every file but the link,
    # `staged` naming an older set, staged/ empty because the signer took that set.
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    assert go(lab) == 0
    sign(state)
    (state / "published" / PI / LINK).unlink()
    stale = json.loads((state / "state.json").read_text())
    stale["staged"] = ["packages/homelab-updater"]
    (state / "state.json").write_text(json.dumps(stale))

    edit(origin, "daniel-box", "box 1")
    assert go(lab) == 0
    assert "homelab-daniel-pi" in debs(state)
    assert (state / "published" / PI / LINK).is_symlink()
    # Accept pair: with the link published, the next commit leaves the Pi alone.
    sign(state)
    edit(origin, "daniel-box", "box 2")
    assert go(lab) == 0
    assert "homelab-daniel-pi" not in debs(state)


def test_a_deferred_package_leaves_published_so_it_builds_when_the_gate_closes(lab):
    origin, state, etc, _ = lab
    edit(origin, "daniel-server", "server 1")
    assert go(lab) == 0, "one changed host package opens no gate"
    assert "homelab-daniel-server" in debs(state)
    # No sign(): the signer has not taken the server's deb when the next commit lands.
    edit(origin, "daniel-pi", "pi 1")
    gate_sha = edit(origin, "daniel-box", "box 1")
    assert go(lab) == 0
    assert debs(state) == ["homelab-daniel-pi", "homelab-updater"], "canary alone"
    assert not (state / "published" / "hosts" / "daniel-server").exists()
    sign(state)

    (etc / "release").write_text(gate_sha + "\n")
    assert go(lab) == 0
    # The server is the canary of the gate this run opens, and the box waits behind it.
    assert debs(state) == ["homelab-daniel-server"]


class FailsAtStage(LinkedBuilder):
    """A run whose stage step raises, as the dangling-link copytree did on 2026-10-23."""

    def stage(self, chosen, out, debs, ver) -> None:
        raise OSError("dangling symlink in the render")


def test_a_failing_stage_step_reaches_stderr_with_its_type_and_step(lab, capsys):
    # #6074: the journal held only systemd's lines, so the cause had to be reproduced by hand.
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    assert go(lab, cls=FailsAtStage) == 1
    err = capsys.readouterr().err
    assert "build: OSError: dangling symlink in the render" in err
    assert "[step after_fetch>build>stage]" in err
    assert metric(state)[0] == "build", "the refusal reason is unchanged"


def test_a_successful_run_writes_nothing_to_stderr(lab, capsys):
    origin, state, _, _ = lab
    edit(origin, "daniel-pi", "pi 1")
    assert go(lab) == 0
    assert capsys.readouterr().err == ""
    assert metric(state)[0] == "ok"


def test_the_token_is_masked_in_a_logged_failure():
    exc = RuntimeError("git fetch https://x:ghp_secret@github.com failed")
    assert "ghp_secret" not in hb.describe(exc, "ghp_secret")
