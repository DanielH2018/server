"""`promote.py land-wait`: wait until a promotion covers a merge commit, or a hold, or a timeout.

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. `Sim`,
in `_land_wait_fakes.py`, answers every read `land_wait` makes. The deadline is tested in
test_promote_land_wait_deadline.py.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_land_wait.py
"""

import io
import json
import tarfile
from dataclasses import replace

import pytest

import forge
from changed_set import changed_set
from deploy_tools.promote_lib import wait as promote_wait
from flux_host import FluxHost
from _land_wait_fakes import D, M, P, Sim, at, digests, kustomization
from _promote_fakes import FakeTransport, digest_of


# -- the three outcomes ----------------------------------------------------------------------


def test_a_promotion_at_the_merge_commit_settles():
    result = Sim().wait()
    assert result.verdict == "settled" and result.revision == M


def test_an_evicted_run_whose_descendant_settles_settles():
    """M's own run was evicted, so no promotion ever applied M itself; D's covers it."""
    sim = Sim(polls=[at(P), at(P), at(D)])
    result = sim.wait()
    assert result.verdict == "settled" and result.revision == D
    assert sim.sleeps == 2


def test_a_hold_set_before_the_merge_ends_the_wait_at_the_first_poll():
    sim = Sim(polls=[at(P)], holds=[f"{P} failed=bento-pdf"])
    result = sim.wait()
    assert (result.verdict, result.cause) == ("deploy-failed", "promote-held")
    assert P in result.detail and "bento-pdf" in result.detail
    assert sim.sleeps == 0


def test_a_hold_set_during_the_wait_ends_it_as_deploy_failed():
    sim = Sim(polls=[at(P)], holds=["none", "none", "held by H1 for a handback"])
    result = sim.wait()
    assert result.cause == "promote-held" and sim.sleeps == 2


@pytest.mark.parametrize("suspended", [True, False])
def test_a_suspended_tag_ends_the_wait_at_once_where_unsuspended_it_gives_up(suspended):
    """The red half is the same fixture with the tag not suspended: nothing applies M, so the
    wait polls to `gave-up`."""
    entry = {"own": "own-b", "footprint": "b", "suspended": suspended}
    sim = Sim(
        polls=[at(P)], published={M: {"bento-pdf": entry}, P: digests(bento_pdf="a")}
    )
    result = sim.wait()
    if suspended:
        assert (result.verdict, result.cause) == ("suspended", "promote-suspended")
        assert "bento-pdf" in result.detail and sim.sleeps == 0
    else:
        assert result.verdict == "gave-up" and sim.sleeps > 0


def test_a_change_that_raced_the_gate_onto_a_suspended_tag_waits_for_the_hold():
    """M changes bento-pdf while it is suspended at M and at its ancestor, so `decide` holds
    and the wait ends `deploy-failed` there rather than `suspended` here."""
    before = {"own": "own-a", "footprint": "a", "suspended": True}
    after = {"own": "own-b", "footprint": "b", "suspended": True}
    sim = Sim(
        polls=[at(P)],
        holds=["none", f"{M} suspended-changed=bento-pdf"],
        published={M: {"bento-pdf": after}, P: {"bento-pdf": before}},
    )
    result = sim.wait()
    assert (result.verdict, result.cause) == ("deploy-failed", "promote-held")
    assert sim.sleeps == 1


def test_a_hold_beside_a_suspended_tag_ends_the_wait_deploy_failed():
    entry = {"own": "own-b", "footprint": "b", "suspended": True}
    sim = Sim(
        polls=[at(P)],
        holds=[f"{P} failed=texbrain"],
        published={M: {"bento-pdf": entry}, P: digests(bento_pdf="a")},
    )
    result = sim.wait()
    assert (result.verdict, result.cause) == ("deploy-failed", "promote-held")


def test_a_suspended_tag_beside_another_service_waits_on_the_other_alone():
    """bento-pdf never applies M, so the wait settles once texbrain and homelab do."""
    entry = {"own": "own-b", "footprint": "b", "suspended": True}
    sim = Sim(
        polls=[
            at(P),
            at(M, "texbrain") + [kustomization("bento-pdf", f"master@sha1:{P}")],
        ],
        published={
            M: {"bento-pdf": entry, **digests(texbrain="t2")},
            P: digests(bento_pdf="a", texbrain="t"),
        },
    )
    result = sim.wait(services=("bento-pdf", "texbrain"))
    assert result.verdict == "settled" and result.suspended == ("bento-pdf",)
    assert sim.sleeps == 1


def test_a_suspended_tag_outside_the_prs_services_does_not_end_the_wait():
    entry = {"own": "own-x", "footprint": "x", "suspended": True}
    sim = Sim(published={M: {"texbrain": entry}, P: {"texbrain": entry}})
    assert sim.wait(services=("bento-pdf",)).verdict == "settled"


def test_no_promotion_within_the_timeout_gives_up():
    sim = Sim(polls=[at(P)])
    result = sim.wait()
    assert (result.verdict, result.cause) == ("gave-up", "promote-timeout")
    assert sim.sleeps == promote_wait.LAND_WAIT_TIMEOUT_S // promote_wait.POLL_S
    assert "publish" in result.detail


def test_an_unreadable_hold_keeps_the_wait_going():
    sim = Sim(polls=[at(P), at(M)], holds=[forge.ForgeError("gh timed out"), "none"])
    assert sim.wait().verdict == "settled"


def test_an_unreadable_kustomization_list_never_reads_as_settled():
    sim = Sim(polls=[forge.ForgeError("kubectl exited 1")])
    assert sim.wait().verdict == "gave-up"


# -- the soak's verdict (D5.3b3) -------------------------------------------------------------

# M changes bento-pdf, so its changed set holds `soak` as C2 puts it beside any tag.
SOAK_PUBLISHED = {
    M: digests(bento_pdf="b", soak="k"),
    P: digests(bento_pdf="a", soak="k"),
}
for _found in SOAK_PUBLISHED.values():
    _found["bento-pdf"]["suspended"] = False


def _soak_sim(**kw) -> Sim:
    return Sim(polls=[at(M, "bento-pdf")], published=SOAK_PUBLISHED, **kw)


def test_the_fixture_changed_set_holds_soak():
    """Without this every case below would settle on a set the soak never joined."""
    found = changed_set(SOAK_PUBLISHED[M], SOAK_PUBLISHED[P], {"soak": []})
    assert "soak" in found.names


def test_every_tag_ready_and_no_flux_soak_status_keeps_waiting():
    """The red half against a wait that reads only the tags: it would settle at the first poll."""
    sim = _soak_sim()
    result = sim.wait()
    assert result.verdict == "gave-up" and sim.annotations == []
    assert sim.soak_reads and set(sim.soak_reads) == {M}


def test_a_pending_flux_soak_keeps_waiting():
    sim = _soak_sim(soak={M: ["pending"]})
    assert sim.wait().verdict == "gave-up"


def test_a_flux_soak_success_on_r_settles():
    sim = _soak_sim(soak={M: [None, "pending", "success"]})
    result = sim.wait()
    assert (result.verdict, result.revision) == ("settled", M)
    assert sim.sleeps == 2 and len(sim.annotations) == 1


@pytest.mark.parametrize("state", ["failure", "error"])
def test_a_flux_soak_failure_on_r_ends_as_soak_failed_before_any_hold(state):
    sim = _soak_sim(soak={M: [state]})
    result = sim.wait()
    assert (result.verdict, result.cause) == ("deploy-failed", "soak-failed")
    assert "app=soak" in result.detail and "terminated.message" in result.detail
    assert sim.sleeps == 0 and sim.annotations == []


def test_a_flux_soak_success_on_another_commit_keeps_waiting():
    """D covers M only once Flux applies D; M's success says nothing about R = D."""
    sim = _soak_sim(soak={M: ["success"]})
    sim.polls = [at(D, "bento-pdf")]
    assert sim.wait().verdict == "gave-up"
    assert set(sim.soak_reads) == {D}


def test_an_unreadable_flux_soak_keeps_waiting_and_never_settles():
    sim = _soak_sim(soak={M: [forge.ForgeError("HTTP 502"), "success"]})
    result = sim.wait()
    assert result.verdict == "settled" and sim.sleeps == 1


# -- the changed set -------------------------------------------------------------------------


def _changed_set_sim(**kwargs: object) -> Sim:
    """bento-pdf's footprint moved between P and M; sonarr's did not and is not Ready."""
    polls = [[*at(M, "bento-pdf"), kustomization("sonarr", f"master@sha1:{P}", False)]]
    published = {
        M: digests(bento_pdf="b", sonarr="s", _unclassified="u"),
        P: digests(bento_pdf="a", sonarr="s", _unclassified="u"),
    }
    return replace(Sim(polls=polls, published=published), **kwargs)


def test_an_unchanged_tag_that_is_not_ready_does_not_block_settled():
    result = _changed_set_sim().wait()
    assert result.verdict == "settled" and not result.widened


def test_a_changed_tag_that_is_not_ready_blocks_settled():
    sim = _changed_set_sim(
        polls=[[*at(M), kustomization("bento-pdf", f"master@sha1:{M}", False)]]
    )
    assert sim.wait().verdict == "gave-up"


def test_a_soak_kustomization_not_ready_is_never_waited_on_its_status_decides():
    """`soak` reads Ready only after its Job judged, so the wait reads `flux/soak` instead."""
    polls = [[*at(M, "bento-pdf"), kustomization("soak", f"master@sha1:{M}", False)]]
    sim = Sim(polls=polls, published=SOAK_PUBLISHED, soak={M: ["success"]})
    assert sim.wait().verdict == "settled"


def test_an_ancestor_with_no_digests_json_waits_on_every_name():
    sim = _changed_set_sim()
    sim.published[P] = promote_wait.WaitReadError("sha-P holds no digests.json")
    result = sim.wait()
    assert result.verdict == "gave-up"
    assert "the ancestor's digests.json" in result.widened


def test_a_merge_not_yet_published_waits_on_every_name_until_it_is():
    """Publish follows CI, so `sha-<M>` is usually absent when the wait starts."""
    sim = _changed_set_sim(published_from_poll=2)
    result = sim.wait()
    assert result.verdict == "settled" and not result.widened
    assert sim.sleeps == 2


# -- names forced past the digests (D3.4) ----------------------------------------------------


def _cluster_vars_sim() -> Sim:
    """A `cluster-vars.source`-only change: no digest moves between P and M.

    `cluster-vars` reaches M on the second poll and its dependent `peanut` on the third.
    `sonarr` depends on nothing and stays at P throughout.
    """

    def poll(cluster_vars: str, peanut: str) -> list[dict]:
        return [
            kustomization("homelab", f"master@sha1:{M}"),
            kustomization("cluster-vars", f"master@sha1:{cluster_vars}"),
            kustomization(
                "peanut", f"master@sha1:{peanut}", depends_on=("cluster-vars",)
            ),
            kustomization("sonarr", f"master@sha1:{P}"),
        ]

    same = digests(cluster_vars="c", peanut="p", sonarr="s")
    return Sim(polls=[poll(P, P), poll(M, P), poll(M, M)], published={M: same, P: same})


def test_a_forced_name_waits_on_it_and_on_its_dependent():
    sim = _cluster_vars_sim()
    result = promote_wait.land_wait(
        sim.reads(), M, [], also=frozenset({"cluster-vars"})
    )
    assert (result.verdict, result.revision) == ("settled", M)
    assert sim.sleeps == 2


def test_no_forced_name_waits_on_the_digests_alone():
    """The control for the test above, not a test of `also`.

    The same simulation settles at the first poll without `also`, so the two sleeps above
    come from `also` and not from the digests or from `sonarr`.
    """
    sim = _cluster_vars_sim()
    result = promote_wait.land_wait(sim.reads(), M, [])
    assert result.verdict == "settled" and sim.sleeps == 0


# -- the revision read -----------------------------------------------------------------------


def test_an_origin_revision_descending_from_the_merge_settles():
    assert Sim(polls=[at(D)]).wait().revision == D


def test_an_absent_origin_revision_keeps_waiting():
    assert Sim(polls=[[kustomization("homelab")]]).wait().verdict == "gave-up"


def test_a_last_applied_revision_without_an_origin_revision_keeps_waiting():
    digest = "prod@sha256:" + "0" * 64
    polls = [[kustomization("homelab", applied=digest)]]
    assert Sim(polls=polls).wait().verdict == "gave-up"


def test_an_origin_revision_the_checkout_has_not_fetched_keeps_waiting():
    assert Sim(polls=[at(D)], unfetched={D}).wait().verdict == "gave-up"


@pytest.mark.parametrize(
    "revision",
    [None, "master@sha1:abc", "prod@sha256:" + "0" * 64, "main@sha1:" + "a" * 40],
)
def test_only_a_master_sha1_origin_revision_names_a_commit(revision):
    assert promote_wait.origin_commit(kustomization("homelab", revision)) is None


# -- the annotation --------------------------------------------------------------------------


def test_settled_writes_one_promote_annotation_naming_the_published_tags():
    sim = Sim(polls=[at(D)])
    sim.wait(services=("bento-pdf", "it-tools"))
    assert sim.annotations == [
        f"event=deploy services=bento-pdf,it-tools sha={D[:8]} result=ok source=promote"
    ]


@pytest.mark.parametrize(
    "sim",
    [Sim(polls=[at(P)], holds=[f"{P} failed=bento-pdf"]), Sim(polls=[at(P)])],
    ids=["deploy-failed", "gave-up"],
)
def test_deploy_failed_and_gave_up_write_no_annotation(sim):
    assert sim.wait().verdict != "settled"
    assert sim.annotations == []


def test_the_exit_codes_are_land_sh_s(capsys):
    assert (
        promote_wait.report_wait(M, promote_wait.WaitResult("settled", revision=D)) == 0
    )
    assert promote_wait.report_wait(M, promote_wait.WaitResult("deploy-failed")) == 1
    assert promote_wait.report_wait(M, promote_wait.WaitResult("gave-up")) == 75
    assert capsys.readouterr().out.splitlines()[-1].startswith("verdict=gave-up ")


# -- the forge reads -------------------------------------------------------------------------


def _bundle(files: dict[str, bytes]) -> bytes:
    """A gzipped tarball shaped as `flux push artifact` writes one, paths under `./`."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


class BlobTransport(FakeTransport):
    """`FakeTransport` plus one layer blob, served at its digest."""

    def __init__(self, layer: bytes):
        super().__init__()
        self.layer = layer
        self.layer_digest = digest_of("layer")
        self.tags[f"sha-{M}"] = digest_of(M)
        self.manifests[f"sha-{M}"] = {"layers": [{"digest": self.layer_digest}]}

    def __call__(self, method, url, headers):
        if url.endswith(f"/blobs/{self.layer_digest}"):
            self.requests.append((method, url))
            return forge.Response(200, body=self.layer)
        return super().__call__(method, url, headers)


def test_the_digests_file_is_read_out_of_the_bundle_layer():
    body = json.dumps(digests(bento_pdf="b")).encode()
    transport = BlobTransport(
        _bundle({"flux/kustomization.yaml": b"", "digests.json": body})
    )
    registry = forge.Registry(transport, None)
    files = registry.bundle(f"sha-{M}")
    assert files is not None and files["digests.json"] == body
    assert registry.bundle(f"sha-{P}") is None


def test_a_bundle_without_the_digests_file_is_a_read_error():
    transport = BlobTransport(_bundle({"flux/kustomization.yaml": b""}))
    f = forge.Forge(
        forge.Registry(transport, None), forge.GitHub(transport, None), forge.Cosign()
    )
    with pytest.raises(promote_wait.WaitReadError, match=r"holds no digests\.json"):
        promote_wait._bundle_digests(f, M)


def test_the_kustomization_list_is_read_with_the_read_only_kubectl():
    import subprocess

    seen = []

    def kubectl(cluster, *args, timeout):
        seen.append((cluster, args))
        return subprocess.CompletedProcess(args, 0, json.dumps({"items": at(M)}), "")

    assert FluxHost(kubectl=kubectl).kustomizations() == at(M)
    assert seen == [
        (
            "prod",
            (
                "get",
                "kustomizations.kustomize.toolkit.fluxcd.io",
                "-n",
                "flux-system",
                "-o",
                "json",
            ),
        )
    ]
