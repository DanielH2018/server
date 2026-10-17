"""The simulated cluster, registry and DEPLOY_HOLD every `land-wait` test reads.

`Sim` answers every read `land_wait` makes and scripts what the cluster shows poll by poll.
The `digests.json` fixtures are in contract C1's format.
"""

from dataclasses import dataclass, field

import chained_timeout
from deploy_tools.promote_lib import verify_promotion as vp
from deploy_tools.promote_lib import wait as promote_wait
from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib.bundle import bundle_files as render_bundle

M = "a" * 40  # the merge commit
P = "9" * 40  # M's first parent, published, which `prod` named before
D = "d" * 40  # a descendant of M, which a later promotion applied
# What each commit descends from, itself included.
ANCESTRY = {M: {M, P}, P: {P}, D: {D, M, P}}


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
        in_flight: what each poll's in-flight read answers, the last one repeating: the
            commit `prod` names while it differs from `last-good`, or None.
        soak: each commit's `flux/soak` state, by commit, as a list a read takes one answer
            from each time, the last one repeating. A commit absent here has no status; an
            exception is raised in place of a state.
    """

    polls: list = field(default_factory=lambda: [at(M)])
    holds: list = field(default_factory=lambda: ["none"])
    published: dict = field(default_factory=dict)
    published_from_poll: int = 0
    unfetched: set = field(default_factory=set)
    in_flight: list = field(default_factory=lambda: [None])
    soak: dict = field(default_factory=dict)
    soak_reads: list = field(default_factory=list)
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

    def _in_flight(self):
        answer = self.in_flight[min(self.poll - 1, len(self.in_flight) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _soak(self, sha):
        self.soak_reads.append(sha)
        answers = self.soak.get(sha) or [None]
        answer = answers[min(self.soak_reads.count(sha) - 1, len(answers) - 1)]
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
            in_flight=self._in_flight,
            soak=self._soak,
            annotate=self.annotations.append,
            sleep=self._sleep,
            clock=lambda: float(self.sleeps * promote_wait.POLL_S),
        )

    def wait(self, services=("bento-pdf",)) -> promote_wait.WaitResult:
        return promote_wait.land_wait(self.reads(), M, list(services))


def substituting_promote_timeout() -> int:
    """`verify`'s wait on the bundle the renderer writes for `peanut` substituting at 10m.

    Every Kustomization in it counts as changed, as a rotation of a key peanut reads changes
    `cluster-vars` and peanut together. Its path runs through the renderer's `cluster-vars`
    edge, so it is the longest wait any one-tag bundle makes today.
    """
    directories = {"peanut": {"kustomization.yaml": "resources: []\n"}}
    files = render_bundle(
        directories,
        {"peanut": frozenset({"peanut_password"})},
        ["peanut"],
        "10m",
        "x=ENC[y]\n",
    )
    docs = vp.flux_kustomizations({p: t.encode() for p, t in files.items()})
    names = {d["metadata"]["name"] for d in docs}
    return chained_timeout.promote_timeout(docs, names)


def publish_pipeline_s() -> int:
    """The longest the `promote` workflow's `render` and `publish` jobs may run, in seconds.

    A promotion of M cannot start before both finish, so `land-wait` needs this much over
    `verify`'s wait. The `promote` job's own budget is left out: most of it is `decide`'s wait
    on a verification already in flight.
    """
    jobs = yaml_fast.safe_load(
        (REPO / ".github/workflows/promote.yml").read_text(encoding="utf-8")
    )["jobs"]
    return sum(int(jobs[job]["timeout-minutes"]) * 60 for job in ("render", "publish"))
