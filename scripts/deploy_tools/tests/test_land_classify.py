"""Resolving the merge commit and classifying what the PR reaches, before any wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_classify.py
"""

import dataclasses

import pytest
import yaml

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools.land_lib import land_tags
from deploy_tools.land_lib import classify
from deploy_tools.land_lib.landing import Classification
from deploy_tools.land_lib.outcome import Outcome


def test_resolve_records_the_merge_sha_and_fetches_master(landing):
    ln, calls = landing()
    classify.resolve(ln)
    assert ln.merge_sha == MERGE_SHA and ln.ledger.merge_sha == MERGE_SHA
    assert ln.ledger.t_merged is not None
    assert ("git", ("fetch", "-q", "origin", "master"), {"cwd": PRIMARY}) in calls


def test_a_pr_with_no_merge_commit_dies(landing):
    ln, _ = landing(Fakes(gh_views={"mergeCommit": {"mergeCommit": None}}))
    with pytest.raises(Outcome) as exc:
        classify.resolve(ln)
    assert exc.value.rc == 1 and "no merge commit" in exc.value.error


def test_the_range_comes_from_the_pull_ref_and_its_merge_base(landing):
    """Not `--since` (other sessions' work) and not `MERGE_SHA^` (wrong for a rebase merge)."""
    ln, calls = landing()
    ln.merge_sha = MERGE_SHA
    assert classify.pr_range(ln) == "prbase..prhead"
    git = [c[1] for c in calls if c[0] == "git"]
    assert ("fetch", "-q", "origin", "refs/pull/999/head") in git
    assert ("merge-base", "prhead", MERGE_SHA) in git


def test_an_unreadable_range_is_empty_and_says_so(landing, capsys):
    """Empty keeps every comment-only edit loud -- the direction a wrong answer must fall."""
    ln, _ = landing(Fakes(pull_ref_rc=1))
    assert classify.pr_range(ln) == ""
    assert "no comment-only edit is read as quiet" in capsys.readouterr().out


def test_classify_fills_tags_and_plane(landing):
    ln, _ = landing(
        Fakes(
            plane="secrets.yml changed",
            derived=(["sonarr", "radarr"], "pr"),
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert (ln.tags_csv, ln.plane, ln.needs_diff) == (
        "sonarr,radarr",
        "secrets.yml changed",
        False,
    )


def test_a_rotation_that_refreshed_every_copy_classifies_with_no_plane(landing):
    """The PR's own range reaches `plane_note`, which a blanket note would ignore (#8369)."""
    ranges = []

    def read_stale(old, new, deployed):
        ranges.append((old, new, deployed))
        return {}

    ln, _ = landing(
        Fakes(
            gh_views={
                "files,changedFiles": {
                    "files": [{"path": "secrets/secrets.yml"}],
                    "changedFiles": 1,
                }
            }
        )
    )
    ln.classifier = dataclasses.replace(
        ln.classifier,
        plane_note=lambda paths, range_="", deployed="": land_tags.plane_note(
            paths, range_, deployed, read_stale=read_stale
        ),
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ranges == [("prbase", "prhead", MERGE_SHA)]
    assert ln.plane == ""


def test_classify_records_the_derived_tags(landing):
    ln, _ = landing(Fakes(derived=(["sonarr", "radarr"], "pr")))
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.tags_csv == "sonarr,radarr"


# ── a role this PR registers is not a role somebody forgot to register ──

_NEW_ROLE_PR = {
    "files": [
        {"path": "k8s/roles/pihole-exporter/tasks/main.yml"},
        {"path": "inventory/host_vars/daniel-box.yml"},
    ],
    "changedFiles": 2,
}


def _real_derivation(ln):
    """Swap in the REAL plane_note and derive, keeping every other classifier faked."""
    ln.classifier = dataclasses.replace(
        ln.classifier, plane_note=land_tags.plane_note, derive=land_tags.derive
    )


def test_a_role_this_pr_registers_is_deployed_rather_than_reported_unregistered(
    landing,
):
    """The entry is at the merge commit and in no checkout yet."""
    ln, _ = landing(
        Fakes(
            gh_views={"files,changedFiles": _NEW_ROLE_PR},
            declared_at={"pihole-exporter"},
        )
    )
    _real_derivation(ln)
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.plane == ""
    assert ln.resolved_tags == ["pihole-exporter"]


def test_a_role_the_merge_commit_does_not_declare_owes_no_hand(landing):
    """It has no tag and no caller, and no hand can apply it, so the note names nothing.

    The `every-k8s-role-has-an-entry-or-a-caller` census row refuses such a role before merge.
    """
    ln, _ = landing(
        Fakes(gh_views={"files,changedFiles": _NEW_ROLE_PR}, declared_at=set())
    )
    _real_derivation(ln)
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.resolved_tags == []
    assert ln.plane == ""


def test_an_unreadable_merge_commit_says_so_and_falls_back_to_this_checkout(
    landing, capsys
):
    """None restores the previous answer -- this checkout's -- rather than declaring nothing
    exists, and the landing says which source it used."""
    ln, _ = landing(
        Fakes(gh_views={"files,changedFiles": _NEW_ROLE_PR}, declared_at=None)
    )
    _real_derivation(ln)
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert "classifying against this checkout instead" in capsys.readouterr().out
    registered_here = "pihole-exporter" in land_tags.declared_tags()
    assert (ln.resolved_tags == ["pihole-exporter"]) is registered_here


# ── a role directory the merge commit lacks is deleted, not shared (#7108) ──


def _role_pr(role: str) -> dict:
    return {
        "files": [{"path": f"k8s/roles/{role}/tasks/main.yml"}],
        "changedFiles": 1,
    }


def test_explicit_tags_skip_derivation(landing):
    """`--tags` overrides the derivation; the override's sonarr survives `derived`'s radarr."""
    ln, _ = landing(Fakes(derived=(["radarr"], "pr")), tags="sonarr")
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.tags_csv == "sonarr"


def test_a_truncated_file_list_without_since_is_a_usage_error(landing):
    from lib.exit_codes import LAND_BAD_ARGS

    ln, _ = landing(Fakes(derived=([], "fallback")))
    ln.merge_sha = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert exc.value.rc == LAND_BAD_ARGS and "--since" in exc.value.error


def test_a_truncated_file_list_with_since_defers_derivation(landing, capsys):
    ln, _ = landing(Fakes(derived=([], "fallback")), since="abc")
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.needs_diff and ln.resolved_tags == []
    assert "deriving from the diff since abc in step 4" in capsys.readouterr().out


def test_nothing_to_deploy_when_nothing_is_reached(landing):
    ln, _ = landing()
    with pytest.raises(Outcome) as exc:
        classify.shortcut_if_nothing(ln)
    assert (exc.value.rc, exc.value.verdict) == (0, "nothing-to-deploy")


@pytest.mark.parametrize(
    "attr, value",
    [
        ("resolved_tags", ["x"]),
        ("plane", "x"),
        ("needs_diff", True),
    ],
)
def test_any_reach_disables_the_shortcut(landing, attr, value):
    ln, _ = landing()
    if attr in {f.name for f in dataclasses.fields(Classification)}:
        ln.classification = Classification(**{attr: value})
    else:
        setattr(ln, attr, value)
    classify.shortcut_if_nothing(ln)


@pytest.mark.parametrize(
    "attr, label",
    [
        ("plane_note", "plane classification failed"),
        ("derive", "tag derivation failed"),
    ],
)
def test_a_crashing_classification_helper_dies_named_rather_than_traces(
    landing, attr, label
):
    """An unhandled exception (a `yaml.YAMLError` reading `host_vars`, say) must still read as
    `land: <name> failed` rather than a bare traceback."""
    ln, _ = landing()
    ln.merge_sha = MERGE_SHA

    def boom(*a, **k):
        raise yaml.YAMLError("bad host_vars")

    ln.classifier = dataclasses.replace(ln.classifier, **{attr: boom})
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert exc.value.rc == 1 and label in exc.value.error


def test_a_classification_helper_calling_die_itself_is_not_relabelled(landing):
    """`Outcome` (what `ln.die` raises) must pass through `_classified` unfiltered."""
    ln, _ = landing()
    ln.merge_sha = MERGE_SHA

    def dies(*a, **k):
        ln.die("something else entirely", 1)

    ln.classifier = dataclasses.replace(ln.classifier, plane_note=dies)
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert exc.value.error == "something else entirely"


def test_a_truncated_file_list_without_since_exits_as_bad_arguments(landing):
    """It is a usage error, so it takes land.sh's documented usage code, 64 (#4451)."""
    from lib.exit_codes import LAND_BAD_ARGS

    ln, _ = landing(Fakes(derived=([], "fallback")))
    ln.merge_sha = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert "--since" in exc.value.error
    assert exc.value.rc == LAND_BAD_ARGS


def test_land_sh_exits_64_on_a_truncated_file_list_without_since(land_run):
    """64 is in the handoff's LAND_CODES, so the same refusal exits 64 direct and handed off."""
    from lib.exit_codes import LAND_BAD_ARGS

    rc, _, err, *_ = land_run([], Fakes(derived=([], "fallback")))
    assert "--since" in err
    assert rc == LAND_BAD_ARGS
