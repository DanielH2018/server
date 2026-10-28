"""Every landing reaches land-wait, and the changed set names the tags it gates (D6.12).

`land.sh` derived its k8s tags from the PR's paths until D6.12, and a deploy-plane PR waited
on the tick's `ansible/deploy.yml` receipt instead. The tick applies no `deploy.yml` since,
so each PR here must reach `promote.py land-wait`, and the tags its `source=promote`
annotation and its health gate name come from the two `digests.json` files.

Every boundary is a `_land_fakes.py` fake except land-wait, which is the real
`promote_wait.land_wait` over `_land_wait_fakes.Sim`. The classifier answers what the real
`land_tags` answers for each PR's paths, so the tree under test decides whether a PR reaches
the wait. The plan is docs/target-architecture/deploy/d6-delete-old-path.md, *`land.sh`'s k8s
tags after D6*.

Run: uv run pytest scripts/deploy_tools/tests/test_land_waits_on_the_changed_set.py
"""

from dataclasses import replace

import pytest

from _land_fakes import MERGE_SHA, PRIMARY, Fakes, build_classifier, build_tools
from _land_wait_fakes import M, P, Sim, at, digests
from deploy_tools.land_lib import land_tags
from deploy_tools.promote_lib import wait as promote_wait

FLUX = ("authelia", "bento-pdf", "texbrain")


@pytest.fixture
def land_run_main(monkeypatch, capsys):
    """`land.main(["--pr", "999"])` over the given tools and classifier; (rc, stdout)."""
    import land

    def run(tools, classifier):
        monkeypatch.setenv("LAND_PRIMARY", str(PRIMARY))
        rc = land.main(["--pr", "999"], tools=tools, classifier=classifier)
        return rc, capsys.readouterr().out

    return run


def _land(land_run_main, paths: list[str], sim: Sim):
    """Land a PR changing `paths`, with land-wait reading `sim`; (rc, out, calls).

    The landing's merge commit is `MERGE_SHA`; the simulated registry names it `M`.
    """
    fakes = Fakes(
        gh_views={
            "files,changedFiles": {
                "files": [{"path": p} for p in paths],
                "changedFiles": len(paths),
            }
        },
        derived=tuple(land_tags.derive(paths, len(paths))),
        self_applied=land_tags.self_applied(paths),
        plane=land_tags.plane_note(paths),
        owners={MERGE_SHA: dict.fromkeys(FLUX, "flux")},
        gate=(True, []),
    )
    tools, calls = build_tools(fakes)

    def land_wait(sha, services, primary, cap_s, window_grace_s):
        calls.append(("land_wait", (sha, services), {}))
        return promote_wait.land_wait(
            sim.reads(), M, services, cap_s=cap_s, window_grace_s=window_grace_s
        )

    tools = replace(tools, flux=replace(tools.flux, land_wait=land_wait))
    rc, out = land_run_main(tools, build_classifier(fakes, calls))
    return rc, out, calls


def _gated(calls) -> list:
    return [c[1][0] for c in calls if c[0] == "gate"]


def _annotation(tags: str) -> list[str]:
    return [f"event=deploy services={tags} sha={M[:8]} result=ok source=promote"]


def test_a_templates_only_pr_reaches_land_wait_and_gates_the_changed_set(
    land_run_main,
):
    """A shared template moves bento-pdf and authelia, and no path names either."""
    sim = Sim(
        polls=[at(M, "authelia", "bento-pdf")],
        published={
            M: digests(authelia="a2", bento_pdf="b2", texbrain="t"),
            P: digests(authelia="a1", bento_pdf="b1", texbrain="t"),
        },
    )
    rc, out, calls = _land(
        land_run_main, ["ansible/templates/workload-shell.yml.j2"], sim
    )
    assert "VERDICT: settled" in out, out
    assert rc == 0
    assert [c[1] for c in calls if c[0] == "land_wait"] == [(MERGE_SHA, [])]
    assert sim.annotations == _annotation("authelia,bento-pdf")
    assert _gated(calls) == [["authelia", "bento-pdf"]]


def test_a_manifests_only_pr_reaches_land_wait_and_gates_the_changed_set(
    land_run_main,
):
    """The shared `manifests` role has no tag of its own; the changed set names its callers."""
    sim = Sim(
        polls=[at(M, "texbrain")],
        published={
            M: digests(authelia="a", bento_pdf="b", texbrain="t2"),
            P: digests(authelia="a", bento_pdf="b", texbrain="t1"),
        },
    )
    rc, out, calls = _land(
        land_run_main, ["ansible/roles/k8s/manifests/tasks/main.yml"], sim
    )
    assert "VERDICT: settled" in out, out
    assert rc == 0
    assert sim.annotations == _annotation("texbrain")
    assert _gated(calls) == [["texbrain"]]


def test_an_inventory_depends_on_edit_waits_on_the_dependent_tag(land_run_main):
    """One entry's `depends_on:` moves only the dependent's `flux/<tag>.yaml`, so only its
    footprint differs: texbrain gains an edge to bento-pdf, and bento-pdf is unchanged."""
    sim = Sim(
        polls=[at(M, "texbrain")],
        published={
            M: digests(bento_pdf="b", texbrain="t-with-edge"),
            P: digests(bento_pdf="b", texbrain="t"),
        },
    )
    rc, out, calls = _land(
        land_run_main, ["ansible/inventory/host_vars/daniel-box.yml"], sim
    )
    assert "VERDICT: settled" in out, out
    assert rc == 0
    assert sim.annotations == _annotation("texbrain")
    assert _gated(calls) == [["texbrain"]]


def test_a_failed_digests_read_waits_on_every_flux_owned_tag_and_says_so(
    land_run_main,
):
    """M's `digests.json` cannot be read, so the set widens to every listed Kustomization.

    The gate takes every `owner: flux` tag among them, and the landing names the widening.
    """
    sim = Sim(
        polls=[at(M, *FLUX)],
        published={
            M: promote_wait.WaitReadError("sha-M holds no digests.json"),
            P: digests(authelia="a", bento_pdf="b", texbrain="t"),
        },
    )
    rc, out, calls = _land(
        land_run_main, ["ansible/templates/workload-shell.yml.j2"], sim
    )
    assert "VERDICT: settled" in out, out
    assert rc == 0
    assert "land-wait waited on every Kustomization: M's digests.json" in out
    assert _gated(calls) == [list(FLUX)]
