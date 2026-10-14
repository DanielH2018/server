"""`promote.py land-wait`: wait until a promotion covers a merge commit, or a hold, or a timeout.

The plan is docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*. `Sim`
answers every read `land_wait` makes and scripts what the cluster shows poll by poll. The
`digests.json` fixtures are in contract C1's format.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_land_wait.py
"""

import io
import json
import tarfile
from dataclasses import dataclass, field, replace

import pytest

import forge
import promote_wait
from flux_host import FluxHost
from _promote_fakes import FakeTransport, digest_of

M = "a" * 40  # the merge commit
P = "9" * 40  # M's first parent, published, which `prod` named before
D = "d" * 40  # a descendant of M, which a later promotion applied
# What each commit descends from, itself included.
ANCESTRY = {M: {M}, P: {P}, D: {D, M, P}}


def kustomization(
    name: str,
    origin: str | None = None,
    ready: bool = True,
    applied: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> dict:
    """One Kustomization as `kubectl get -o json` lists it."""
    status: dict = {
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]
    }
    if origin is not None:
        status["lastAppliedOriginRevision"] = origin
    if applied is not None:
        status["lastAppliedRevision"] = applied
    return {
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {"dependsOn": [{"name": d} for d in depends_on]},
        "status": status,
    }


def at(commit: str, *names: str, ready: bool = True) -> list[dict]:
    """`homelab` and `names`, each applied at `commit`."""
    return [
        kustomization(n, f"master@sha1:{commit}", ready) for n in ("homelab", *names)
    ]


def digests(**footprints: str) -> dict:
    return {
        name.replace("_", "-"): {"own": f"own-{v}", "footprint": v}
        for name, v in footprints.items()
    }


@dataclass
class Sim:
    """The cluster, the registry and DEPLOY_HOLD, poll by poll.

    Attributes:
        polls: what each poll's Kustomization read answers, the last one repeating. An
            exception is raised in its place.
        holds: what each poll's DEPLOY_HOLD read answers, the last one repeating.
        published: each commit's parsed `digests.json`, by commit. A commit absent here has no
            `sha-<commit>` tag; an exception is raised in place of a file.
        published_from_poll: the poll from which `published` answers for M; None before it.
        unfetched: the commits whose ancestry read answers None, as an unfetched R does.
    """

    polls: list = field(default_factory=lambda: [at(M)])
    holds: list = field(default_factory=lambda: ["none"])
    published: dict = field(default_factory=dict)
    published_from_poll: int = 0
    unfetched: set = field(default_factory=set)
    annotations: list = field(default_factory=list)
    sleeps: int = 0
    poll: int = 0

    def _kustomizations(self):
        answer = self.polls[min(self.poll, len(self.polls) - 1)]
        self.poll += 1
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _hold(self):
        answer = self.holds[min(self.poll - 1, len(self.holds) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _is_ancestor(self, ancestor, descendant):
        if descendant in self.unfetched:
            return None
        return ancestor in ANCESTRY.get(descendant, {descendant})

    def _digests(self, sha):
        if sha == M and self.poll - 1 < self.published_from_poll:
            return None
        answer = self.published.get(sha)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _sleep(self, _seconds):
        self.sleeps += 1

    def reads(self) -> promote_wait.WaitReads:
        return promote_wait.WaitReads(
            hold=self._hold,
            kustomizations=self._kustomizations,
            is_ancestor=self._is_ancestor,
            digests=self._digests,
            published_ancestor=lambda sha: P if P in self.published else None,
            annotate=self.annotations.append,
            sleep=self._sleep,
            clock=lambda: float(self.sleeps * promote_wait.POLL_S),
        )

    def wait(self, services=("bento-pdf",)) -> promote_wait.WaitResult:
        return promote_wait.land_wait(self.reads(), M, list(services))


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
    assert registry.bundle_file(f"sha-{M}", "digests.json") == body
    assert registry.bundle_file(f"sha-{P}", "digests.json") is None


def test_a_bundle_without_the_digests_file_is_a_read_error():
    transport = BlobTransport(_bundle({"flux/kustomization.yaml": b""}))
    with pytest.raises(forge.ForgeError, match=r"holds no digests\.json"):
        forge.Registry(transport, None).bundle_file(f"sha-{M}", "digests.json")


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
