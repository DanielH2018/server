"""The five secret rules and the pinned-fact refusal against the real tree (target slice R2).

A fleet render in placeholder mode must pass the rules with exactly `KNOWN_VIOLATIONS`, and
each rule must find its named members, so a rule that stops matching cannot pass. A `--role`
render asserts only the members of the tags it rendered. The fixture pairs for each rule are in
`test_renderer_secret_checks.py`.

Run: uv run pytest scripts/renderer/tests/test_renderer_secret_checks_tree.py
"""

import dataclasses

import pytest

from renderer import homelab_render
from renderer.renderer_lib.cluster import flux_owned_tags, select_tags
from renderer.renderer_lib.placeholders import (
    pinned_fact_refusals,
)
from renderer.renderer_lib.secret_checks import (
    KNOWN_VIOLATIONS,
    NO_SECRET_EXPRESSION,
    NON_SECRET_VARS_LOOKUPS,
    Violation,
    check,
    output_members,
    rule5,
    rule5_members,
)
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS, PinnedFact
from renderer.standalone_map import fleet_render


@pytest.fixture(scope="module")
def render():
    return fleet_render()


@pytest.fixture(scope="module")
def keys():
    return homelab_render.secret_keys()


@pytest.fixture(scope="module")
def rule5_result(keys):
    return rule5(keys)


def test_the_fleet_passes_with_exactly_the_known_violations_is_clean(
    render, keys, rule5_result
):
    report = check(render, keys, rule5_result=rule5_result)
    assert report.lines() == []
    assert set(report.violations) == KNOWN_VIOLATIONS


def test_rule5_finds_its_floor_named_members_and_allowlisted_site_is_clean(
    keys, rule5_result
):
    assert rule5_members(rule5_result, keys) == []
    (site,) = NON_SECRET_VARS_LOOKUPS
    assert (site.template, site.argument) in rule5_result.lookup_sites
    # Neither the lookup nor the hashed set block it sits in is a violation (#4952).
    assert [v for v in rule5_result.violations if v.file == site.template] == []


def _without(render, directory: str, file_part: str, old: str):
    """`render` with `old` deleted from every file of `directory` whose name holds `file_part`."""
    tags = []
    for tag in render.tags:
        if tag.tag == directory:
            files = {
                name: text.replace(old, "") if file_part in name else text
                for name, text in tag.files.items()
            }
            tag = dataclasses.replace(tag, files=files)
        tags.append(tag)
    return dataclasses.replace(render, tags=tags)


@pytest.mark.parametrize(
    ("directory", "file_part", "old", "member"),
    [
        ("traefik", "static-config", "${email}", "${email} in traefik's"),
        ("authelia", "config-secret", "${", "a placeholder in authelia's"),
        ("qbittorrent", "netns", "$${", "an escaped $${ in qbittorrent's"),
    ],
)
def test_an_output_rule_member_removed_from_the_fleet_is_flagged(
    render, keys, directory, file_part, old, member
):
    assert output_members(render, keys) == []
    (missing,) = output_members(_without(render, directory, file_part, old), keys)
    assert member in missing


def test_a_role_render_without_the_named_members_is_clean(tmp_path, capsys):
    # traefik, authelia and qbittorrent are absent, so their members are not asserted.
    out = tmp_path / "out"
    assert (
        homelab_render.main(["cluster", "--out", str(out), "--role", "bento-pdf"]) == 0
    )
    assert "the five secret rules pass" in capsys.readouterr().out


def test_no_render_target_is_a_flux_owned_reader_of_a_pinned_fact_is_clean():
    # CI's pytest job runs this, so the commit that marks a reader `owner: flux` goes red
    # before any publish job could run.
    assert pinned_fact_refusals(select_tags(None), flux_owned_tags()) == []


def test_the_refusal_refuses_every_reader_pinned_live_facts_records():
    # With every render target marked flux-owned, the refusal names each tag that reads a
    # pinned fact. R1's reader test holds PINNED_LIVE_FACTS to the templates.
    targets = select_tags(None)
    refused = {tag for tag, _ in pinned_fact_refusals(targets, targets)}
    assert refused == {tag for f in PINNED_LIVE_FACTS.values() for tag in f.readers}
    # The member at this commit. R3's PRs retire each reader as they drop its facts.
    assert refused == {"authelia"}


def test_a_render_that_breaks_the_ratchet_exits_1_and_writes_nothing(tmp_path, capsys):
    stale = Violation(
        NO_SECRET_EXPRESSION, "shared", "gone.yaml.j2", "filter trim", "k"
    )
    out = tmp_path / "out"
    known = KNOWN_VIOLATIONS | {stale}
    code = homelab_render.cluster(out, ["bento-pdf"], None, known=known)
    assert code == 1
    err = capsys.readouterr().err
    assert f"stale KNOWN_VIOLATIONS entry, remove it: {stale.line()}" in err
    assert "the secret checks failed. Nothing was written." in err
    assert not (out / "cluster").exists()


FIXTURE_FACTS = {
    "fixture_fact": PinnedFact("stub-fixture-fact", frozenset({"bento-pdf"}))
}


def _cluster(tmp_path, owned: set[str], facts: dict) -> int:
    out = tmp_path / "out"
    return homelab_render.cluster(
        out, ["bento-pdf"], None, flux_owned=lambda: owned, pinned=facts
    )


def test_a_flux_owned_tag_reading_a_pinned_fact_is_flagged(tmp_path, capsys):
    assert _cluster(tmp_path, {"bento-pdf"}, FIXTURE_FACTS) == 1
    err = capsys.readouterr().err
    assert "bento-pdf is marked owner: flux" in err and "fixture_fact" in err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("owned", "facts"),
    [(set(), FIXTURE_FACTS), ({"bento-pdf"}, {})],
    ids=["tag-not-flux-owned", "fact-not-pinned"],
)
def test_a_pinned_fact_refusal_without_both_halves_is_clean(tmp_path, owned, facts):
    assert _cluster(tmp_path, owned, facts) == 0
