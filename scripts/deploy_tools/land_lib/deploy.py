"""Step 4: wait on the promotion that applies this PR, or say why there is nothing to wait on.

Every `cluster/services.yml` entry is a k8s service, so `promote` and
Flux apply every tag, and a host's package reaches it through the host wait in step 5.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import promotion
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Cause, Verdict, host_not_waited_note, say


def derive_from_diff(ln: Landing) -> None:
    """Resolve a truncated file list to the tags of the diff from `--since` to the merge.

    The range covers every other session's merged work too, which is wider than the PR: wider
    than the truth is recoverable, and a narrower list would gate nothing on the large-PR path,
    where verification matters most.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.git(
        "diff", "--no-renames", "--name-only", f"{ln.opts.since}..{ln.merge_sha}"
    )
    if r.returncode != 0:
        ln.die(f"git diff {ln.opts.since}..{ln.merge_sha[:12]} failed", 1)
    paths = [p for p in r.stdout.splitlines() if p]
    declared = ln.classification.declared_set()
    ln.resolved_tags = ln.classifier.derive(paths, len(paths), declared).tags


def no_tag_outcome(ln: Landing, scope: str = "no service tag") -> NoReturn:
    """No service tag: owed to a hand, or nothing at all.

    `scope` is why this landing has no tags to deploy, printed on the verdict line. The
    default is the ordinary case — the PR's own files derived none. `derive_from_diff` passes
    its own wording, because a refused broad derivation is not the same fact and an operator
    reading `reaches no service tag` about a 330-file PR goes looking for a different bug.

    Runs before the host wait. A PR that changes a host's package and owes no plane never gets
    here: `deploy_phase` returns, and the host wait ends it `settled`. So `nothing-to-deploy`
    means the PR changed no host package either.
    """
    pr = ln.opts.pr
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
        if ln.host_targets:  # this arm ends the landing before the host wait
            print(host_not_waited_note(ln.host_targets))
        ln.finish(
            Verdict.NEEDS_MANUAL_APPLY,
            1,
            f"PR #{pr} reaches {scope}, but is not done",
        )
    ln.finish(Verdict.NOTHING_TO_DEPLOY, 0, f"PR #{pr} touched no service")


def deploy_phase(ln: Landing) -> None:
    """Step 4, end to end: derive, split, wait on promotion, stamp the ledger.

    The split comes first (`promotion.split_k8s`), and a tag it leaves behind names no k8s entry,
    so nothing can apply it. Every landing whose PR may move the bundle then waits on the
    promotion, and land-wait's changed set names the tags the gate reads. A PR that moves no
    bundle file, or whose changed set names no tag, ends in `no_tag_outcome`. A plane it also
    carries, such as the rotation note for `secrets.yml`, is reported by the health step after a
    settled wait, and by `wait_on_promotion` when the wait ends the landing.
    """
    if ln.needs_diff:
        derive_from_diff(ln)
    promotion.split_k8s(ln)
    if ln.resolved_tags:
        ln.ledger.tags_label = ln.tags_csv
        ln.ledger.cause = Cause.TAG_MISS
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{ln.opts.pr} — {ln.tags_csv} matched no k8s entry at "
            f"{ln.merge_sha[:12]}, so nothing applies it",
        )
    waits = (
        bool(ln.flux_tags)
        or ln.needs_diff
        or promotion.moves_the_bundle(ln.classification.pr_paths)
    )
    if not waits:
        if ln.host_targets and not ln.plane:
            return  # an empty change set, then the host wait
        no_tag_outcome(ln)
    ln.ledger.tags_label = ",".join(ln.flux_tags)
    # The gate renders the merge commit from a snapshot: the primary checkout may not hold it.
    ln.deployed_at = ln.merge_sha
    promotion.wait_on_promotion(ln)
    # Step 5's own time on the board, the promotion wait included.
    ln.ledger.t_deploy = ln.tools.clock()
    if not ln.flux_tags:
        if ln.host_targets and not ln.plane:
            return
        no_tag_outcome(ln)
