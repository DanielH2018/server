"""How long `promote.py verify` waits: the chained timeout (D4.3), capped at VERIFY_CAP_S.

`promote_lib/verify_promotion.py:expected_set` calls `promote_timeout` with T's bundle and its changed set,
the same set `verify` expects, so the timeout and the expected statuses cannot disagree. The
plan is docs/target-architecture/deploy/d4-ordering.md, *What `promote` reads*, and the
operator's view is docs/flux-deploy-plane.md, *The timeout follows the chain*. Like
`changed_set`, it is pure and reaches no forge: the caller reads the bundle.

Run: imported only. `uv run pytest scripts/deploy_tools/tests/test_chained_timeout.py`
"""

import re
import sys as _sys
from collections.abc import Iterable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from changed_set import ROOT, SOAK
from deploy_tools.kustomizations import kustomization_graph

# The values of the two objects the bundle does not carry, from
# ansible/roles/k8s/flux/templates/sync.yaml.j2: the OCIRepository `homelab`'s `interval`, and
# the root Kustomization `homelab`'s `timeout` and `retryInterval`. The root is always
# expected, so they are the floor of every derived timeout.
# scripts/deploy_tools/tests/test_chained_timeout.py fails a drift.
OCI_INTERVAL_S = 60
ROOT_TIMEOUT_S = 5 * 60
ROOT_RETRY_S = 60
# kustomize-controller's `--requeue-dependency` default. A Kustomization whose dependency is
# not Ready requeues after this interval, and no watch re-enqueues it sooner, so each
# `dependsOn` hop costs up to one interval (d4-ordering.md). The flux role sets no such flag;
# scripts/deploy_tools/tests/test_chained_timeout.py fails one it adds.
REQUEUE_DEPENDENCY_S = 30
# The workers kustomize-controller's `--concurrent` leaves the tags: how many it reconciles at
# once. One that waits on health holds a worker, so a changed set larger than this queues in
# rounds. The flux role sets `--concurrent` to `k8s_rollout_batch_width` plus one, the worker
# the soak holds from the start of a rollout (D5.1c, D5.3b2), and
# ansible/tests/k8s/test_flux_role.py fails a drift between that width and this figure.
CONCURRENT = 5
# The ceiling of every derived timeout, 5.5 hours. GitHub stops a hosted job at 6 hours, and a
# `verify` stopped by its job limit leaves no verdict, so `settle` writes `<T> unverified`,
# which names no Kustomization. Capping here makes `verify` print `failed` first, naming the
# names (d4-ordering.md, *The cap*). The three workflow limits are sized from this figure
# (ansible/tests/repo/test_promote_workflow_verify.py).
# DECIDED: the clamp stands although the published bundle's all-changed chain is past it
# (#5868: 32256s with 114 Kustomizations, against 19800s; it was 32016s until D5 step 29 added
# `flux` and `flux-deferred`, and 31920s until Longhorn v1.13.0 raised the `longhorn` timeout
# from 2m to 10m, #5972). `verify`'s own wait is the same
# `promote_timeout`, so it stops at this cap too. Lifting only the soak's rendered wait would
# let the soak outlast a `verify` that has already written `failed=<names>`, and would pass no
# promotion. The soak's wait cannot shrink to the chain of what changed either: the render
# cannot know R's changed set (renderer_lib/soak.py). A narrow revision stays well under the
# cap. A revision that changes every tag at once, such as a renderer change, can fail at the
# cap, and the hold names the Kustomizations that had not settled. The remedy is a `verify`
# that re-enters rather than holding one runner, which is d4-ordering.md's Open question 14.
VERIFY_CAP_S = 330 * 60

_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def seconds(duration: str) -> int:
    """A Go duration of whole hours, minutes and seconds, such as `10m` or `1h30m`, in seconds.

    Raises:
        ValueError: any other form, or an empty one.
    """
    match = _DURATION.fullmatch(duration) if isinstance(duration, str) else None
    if match is None or not duration:
        raise ValueError(f"{duration!r} is not a duration of h, m and s")
    hours, minutes, secs = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + secs


def queueing_s(timeouts: list[int], concurrent: int) -> int:
    """An upper bound on when the last of `timeouts` finishes on `concurrent` workers.

    kustomize-controller's workers take the next queued Kustomization as one finishes, which is
    greedy list scheduling. Two bounds hold for it, and this returns the smaller:

    - rounds: `timeouts` in rounds of `concurrent`, each priced at the largest timeout. It is
      the smaller for a uniform bundle.
    - Graham's: the last job to finish, of length p, starts once every worker is busy no
      later than (sum - p) / concurrent, and so ends by (sum - max) / concurrent + max. It is
      the smaller when one long timeout sits among short ones, which rounds would price
      into every round (#5482).

    `promote_timeout` adds this to the longest `dependsOn` path, and the sum stays a bound
    under precedence: the path is at least the critical path L, and Graham's bound for
    precedence-constrained jobs is sum / concurrent + (1 - 1 / concurrent) * L.
    """
    if not timeouts:
        return 0
    longest = max(timeouts)
    rounds = -(-len(timeouts) // concurrent) * longest
    graham = -(-(sum(timeouts) - longest) // concurrent) + longest
    return min(rounds, graham)


def promote_timeout(
    docs: list[dict],
    changed: Iterable[str] | None = None,
    concurrent: int = CONCURRENT,
    requeue_s: int = REQUEUE_DEPENDENCY_S,
    cap_s: int = VERIFY_CAP_S,
) -> int:
    """How long `verify` waits, in seconds: the chained timeout (D4.3), capped at VERIFY_CAP_S.

    With edges, a Kustomization starts only after its dependencies finish, so the wait is:

    - the longest `dependsOn` path that ends at a changed name. A changed name on it counts
      its own `timeout`. An unchanged ancestor counts none, because its apply changes nothing
      and a healthy one passes its health check at once. Each edge counts one `requeue_s`;
    - a queueing term, since each changed name waiting on health holds a worker. It is the
      smaller of two bounds on when the last one finishes: the changed names divided by
      `concurrent`, rounded up, times the largest `timeout` among them; and Graham's bound
      for greedy list scheduling, the other `timeout`s' sum divided by `concurrent`,
      rounded up, plus the largest (`queueing_s`);
    - the OCIRepository interval and the largest `retryInterval`.

    `soak` carries no edges and holds a worker of its own from the start of the rollout,
    waiting at run time for the rest (D5 Open question 19). So when it is changed its
    `timeout` is a parallel term: the first two terms together never fall below it, and it
    takes no place in the queue.

    The root `homelab` is always expected and is not in the bundle, so its `timeout` is the
    floor of the first two terms together, and its `retryInterval` the floor of the last. A
    Kustomization with no `timeout` times out after its `interval`, as Flux does.

    Args:
        docs: every Kustomization in T's bundle.
        changed: T's changed set, the names `verify` expects less the root. None counts every
            Kustomization in `docs` as changed.
        concurrent: kustomize-controller's `--concurrent`.
        requeue_s: kustomize-controller's `--requeue-dependency`, in seconds.
        cap_s: the ceiling, VERIFY_CAP_S. A caller that measures the chain the cap hides
            passes a larger one.

    Raises:
        ValueError: a duration this module cannot read, a `dependsOn` naming a Kustomization
            the bundle does not hold, which Flux would wait on forever, or a cycle.
    """

    def spec_of(doc: dict) -> dict:
        spec = doc.get("spec")
        return spec if isinstance(spec, dict) else {}

    def timeout_of(spec: dict) -> int:
        timeout = spec.get("timeout", spec.get("interval"))
        return 0 if timeout is None else seconds(timeout)

    timeouts, retries = {}, [ROOT_RETRY_S]
    for doc in docs:
        spec = spec_of(doc)
        timeouts[(doc.get("metadata") or {}).get("name")] = timeout_of(spec)
        retry = spec.get("retryInterval")
        if retry is not None:
            retries.append(seconds(retry))
    depends_on, _ = kustomization_graph(docs)
    changed = set(timeouts) if changed is None else set(changed) - {ROOT}
    for name, deps in depends_on.items():
        missing = sorted(set(deps) - set(depends_on))
        if missing:
            raise ValueError(
                f"{name} depends on {', '.join(missing)}, which the bundle does not hold"
            )

    longest: dict[str, int] = {}

    def path_to(name: str, seen: tuple[str, ...]) -> int:
        """The longest weighted `dependsOn` path that ends at `name`, `name` included."""
        if name in seen:
            raise ValueError(f"the dependsOn edges form a cycle through {name}")
        if name not in longest:
            own = timeouts.get(name, 0) if name in changed else 0
            longest[name] = own + max(
                (requeue_s + path_to(dep, (*seen, name)) for dep in depends_on[name]),
                default=0,
            )
        return longest[name]

    chain = max(
        (path_to(name, ()) for name in changed - {SOAK} if name in depends_on),
        default=0,
    )
    waiting = [timeouts[name] for name in changed - {SOAK} if name in timeouts]
    queueing = queueing_s(waiting, concurrent)
    soak = timeouts.get(SOAK, 0) if SOAK in changed else 0
    total = max(chain + queueing, soak, ROOT_TIMEOUT_S) + OCI_INTERVAL_S + max(retries)
    return min(total, cap_s)
