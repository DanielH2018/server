"""Resolving the merge commit and classifying what the PR reaches, before any wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_classify.py
"""

import dataclasses

import pytest
import yaml

from _land_fakes import MERGE_SHA, PRIMARY, Fakes
from deploy_tools import land_tags
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
    """Empty classifies every broad path as loud -- the direction a wrong answer must fall."""
    ln, _ = landing(Fakes(pull_ref_rc=1))
    assert classify.pr_range(ln) == ""
    assert "every broad path stays owed to a hand" in capsys.readouterr().out


def test_classify_fills_tags_plane_and_self_applied(landing):
    ln, _ = landing(
        Fakes(
            plane="initial_setup.yml --tags k3s",
            self_applied=True,
            derived=(["sonarr", "radarr"], "pr"),
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert (ln.tags_csv, ln.plane, ln.classification.self_applied, ln.needs_diff) == (
        "sonarr,radarr",
        "initial_setup.yml --tags k3s",
        True,
        False,
    )


def test_classify_deploys_the_callers_of_a_shared_role(landing):
    """Only a shared role changed, so the landing deploys its callers."""
    ln, _ = landing(
        Fakes(derived=([], "pr"), shared_callers={"helper": {"sonarr", "radarr"}})
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.tags_csv == "radarr,sonarr"


def test_classify_records_which_tags_only_the_caller_graph_named(landing):
    """Those tags route to a `platform: k8s` entry alone, so the deploy phase has
    to be able to tell them from the ones a changed path named."""
    ln, _ = landing(
        Fakes(
            derived=(["sonarr"], "pr"),
            shared_callers={"helper": {"sonarr", "wg-easy"}},
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert (ln.resolved_tags, ln.k8s_only) == (
        ["sonarr", "wg-easy"],
        ["wg-easy"],
    )


def test_classify_leaves_the_tags_alone_when_no_shared_role_changed(landing):
    ln, _ = landing(Fakes(derived=(["sonarr", "radarr"], "pr"), shared_callers={}))
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.tags_csv == "sonarr,radarr"
    assert ln.k8s_only == []


def test_classify_records_the_tags_a_changed_path_proves_are_k3s(landing):
    """A PR touching only `roles/k8s/wg-easy/` names a tag the Pi also declares,
    and the routing read has to be told the change is the cluster's."""
    ln, _ = landing(Fakes(derived=(["wg-easy"], "pr"), path_k8s_only=["wg-easy"]))
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.k8s_only == ["wg-easy"]


def test_both_provenances_land_in_one_list(landing):
    """The caller graph and the path derivation answer for different tags, and `deploy_by_host`
    routes the union: recording one over the other loses the restriction for the other."""
    ln, _ = landing(
        Fakes(
            derived=(["wg-easy"], "pr"),
            path_k8s_only=["wg-easy"],
            shared_callers={"helper": {"sonarr"}},
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.k8s_only == ["sonarr", "wg-easy"]


def test_a_truncated_file_list_proves_nothing_about_a_platform(landing):
    """REJECTING half: on the FALLBACK path `derive_from_diff` rebuilds the tags in step 5 from
    a diff this classification never reads, so no tag may be restricted here. Step 5 answers
    the same question over the diff's own paths instead (`deploy.record_k8s_only`)."""
    ln, _ = landing(
        Fakes(derived=([], "fallback"), path_k8s_only=["wg-easy"]), since="abc"
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.k8s_only == []


# ── a role this PR registers is not a role somebody forgot to register ──

_NEW_ROLE_PR = {
    "files": [
        {"path": "ansible/roles/k8s/pihole-exporter/tasks/main.yml"},
        {"path": "ansible/inventory/host_vars/daniel-box.yml"},
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


def test_a_role_the_merge_commit_does_not_declare_is_still_reported(landing):
    """The must-fire half: a role nobody registered still owes a hand, and derives no tag."""
    ln, _ = landing(
        Fakes(gh_views={"files,changedFiles": _NEW_ROLE_PR}, declared_at=set())
    )
    _real_derivation(ln)
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert "pihole-exporter" in ln.plane
    assert ln.resolved_tags == []


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


def test_classify_asks_what_a_self_applied_role_still_owes_other_hosts(landing):
    """The tick runs on ONE host, so `self_applied` alone leaves the rest silent."""
    ln, calls = landing(
        Fakes(
            self_applied=True, remaining_setup="`initial_setup` also reaches daniel-pi"
        )
    )
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.classification.remaining_setup == "`initial_setup` also reaches daniel-pi"
    # the host the tick ran on, read from the boundary rather than hardcoded
    assert next(c for c in calls if c[0] == "remaining_setup_hosts")[1] == (
        "daniel-box",
    )


def test_explicit_tags_skip_derivation_but_not_the_self_applied_half(landing):
    """`--tags` overrides the derivation; it does not opt out of the plane classification.

    The file list is read either way, so the derivation being skipped is asserted by its
    ANSWER not landing: `derived` names radarr and the override's sonarr survives.
    """
    ln, _ = landing(Fakes(derived=(["radarr"], "pr"), self_applied=True), tags="sonarr")
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.tags_csv == "sonarr"
    assert ln.classification.self_applied and ln.tick_is_the_apply


def test_a_truncated_file_list_without_since_is_a_usage_error(landing):
    ln, _ = landing(Fakes(derived=([], "fallback")))
    ln.merge_sha = MERGE_SHA
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert exc.value.rc == 2 and "--since" in exc.value.error


def test_a_truncated_file_list_with_since_defers_derivation(landing, capsys):
    ln, _ = landing(Fakes(derived=([], "fallback")), since="abc")
    ln.merge_sha = MERGE_SHA
    classify.classify(ln)
    assert ln.needs_diff and ln.resolved_tags == []
    assert "deriving from the diff since abc after the tick" in capsys.readouterr().out


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
        ("self_applied", True),
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
        ("self_applied", "self-applied classification failed"),
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


def test_remaining_setup_hosts_crashing_dies_named(landing):
    """Separate from the parametrized case: this helper takes `local_host` as a positional
    argument, so it needs `self_applied=True` reached first before it is ever called."""
    ln, _ = landing(Fakes(self_applied=True))
    ln.merge_sha = MERGE_SHA

    def boom(*a, **k):
        raise yaml.YAMLError("bad host_vars")

    ln.classifier = dataclasses.replace(ln.classifier, remaining_setup_hosts=boom)
    with pytest.raises(Outcome) as exc:
        classify.classify(ln)
    assert (
        exc.value.rc == 1
        and "remaining-setup-hosts classification failed" in exc.value.error
    )


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
