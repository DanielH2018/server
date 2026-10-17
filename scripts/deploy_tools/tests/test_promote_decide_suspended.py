"""D5.1d's race in `promote.py decide`: a revision that changes a suspended tag holds.

A PR whose checks ran before a suspend merged can still merge after it, so `decide` holds a
revision whose `own` digest differs from `last-good`'s for a tag suspended at both ends. The
plan is docs/target-architecture/deploy/d5-bulk-move.md, *A suspended role takes no change
(D5.1d)*. The cases run the CLI over the fake forge in `_promote_fakes.py`. The hold's post
(#5359) runs through `promote.main` with a fake sender in place of Discord.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_decide_suspended.py
"""

import forge
import promote
import pytest
from _promote_fakes import World, bundle_files


def _bundle(bento: str, suspended: bool, texbrain: str = "t") -> dict[str, bytes]:
    return bundle_files(
        {
            "bento-pdf": {"own": bento, "footprint": bento, "suspended": suspended},
            "texbrain": {"own": texbrain, "footprint": texbrain, "suspended": False},
            "_unclassified": {"own": "u", "footprint": "u"},
        }
    )


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, size=2)
    base = w.commits[-1]
    w.set_prod(base, files=_bundle("a", suspended=True))
    w.set_last_good(base)
    return w


def _decide(world, monkeypatch, capsys):
    return world.decide(monkeypatch, capsys, trigger=world.commits[-1])


def test_a_revision_that_changes_a_tag_suspended_at_both_ends_holds(
    world, monkeypatch, capsys
):
    r = world.commit("change bento-pdf while it is suspended")
    world.publish(r, files=_bundle("b", suspended=True))
    result = _decide(world, monkeypatch, capsys)
    assert (result.exit_code, result.outputs["verdict"]) == (0, "held"), result.stderr
    assert world.run.written == [("DEPLOY_HOLD", f"{r} suspended-changed=bento-pdf")]
    assert result.outputs["suspended_hold"] == f"{r} suspended-changed=bento-pdf"


def test_only_the_run_that_writes_the_hold_hands_it_to_the_post(
    world, monkeypatch, capsys
):
    r = world.commit("change bento-pdf while it is suspended")
    world.publish(r, files=_bundle("b", suspended=True))
    _decide(world, monkeypatch, capsys)
    again = _decide(world, monkeypatch, capsys)
    assert again.outputs["verdict"] == "held", again.stderr
    assert again.outputs["suspended_hold"] == ""


def test_the_resume_revision_is_promoted(world, monkeypatch, capsys):
    r = world.commit("change bento-pdf and remove flux_suspend")
    world.publish(r, files=_bundle("b", suspended=False))
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert result.outputs["target"] == r and world.run.written == []
    assert result.outputs["suspended_hold"] == ""


def test_a_revision_that_leaves_the_suspended_tag_alone_is_promoted(
    world, monkeypatch, capsys
):
    """A change to another tag, or the revert of the racing change, keeps `own` as it was."""
    r = world.commit("change texbrain")
    world.publish(r, files=_bundle("a", suspended=True, texbrain="t2"))
    result = _decide(world, monkeypatch, capsys)
    assert result.outputs["verdict"] == "promote", result.stderr
    assert world.run.written == []


R = "ab" * 20


def _hold(hold: str, post) -> int:
    return promote.main(["hold", R, "--hold", hold], post=post)


def test_the_hold_posts_its_tags_and_the_way_out():
    sent: list[str] = []
    assert _hold(f"{R} suspended-changed=bento-pdf,texbrain", sent.append) == 0
    (message,) = sent
    assert f"DEPLOY_HOLD set to `{R} suspended-changed=bento-pdf,texbrain`" in message
    assert "changes bento-pdf, texbrain" in message and "prod did not move" in message
    assert "reverts the change or resumes the tag" in message
    assert len(message) <= forge.DISCORD_MAX


def test_a_failed_post_of_the_hold_fails_the_run():
    def refuse(_content: str) -> None:
        raise forge.ForgeError("discord: HTTP 500")

    assert _hold(f"{R} suspended-changed=bento-pdf", refuse) == 1


def test_a_stateful_hold_without_its_record_is_a_usage_error():
    sent: list[str] = []
    assert _hold(f"{R} stateful=sonarr", sent.append) == 64 and sent == []
