"""Step 4: wait on the promotion that applies this PR, or say why there is nothing to wait on.

No landing runs deploy.sh. Every `containers_list` entry is `platform: k8s`, so `promote` and
Flux apply every tag, and a host's package reaches it through the host wait in step 5.
"""

from typing import NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.exit_codes import DEPLOY_BROAD, DEPLOY_OK
from deploy_tools.land_lib import promotion
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Cause, Verdict, host_not_waited_note, say


def derive_from_diff(ln: Landing) -> None:
    """Resolve the fallback to a tag list HERE rather than handing deploy.sh --changed.

    deploy.sh resolves --changed internally, so the verdict call would receive an empty
    --tags and report settled having checked nothing -- on the large-PR path, where
    verification matters most.

    A range `changed` refuses as broad derives no tag here. Its k8s half is the promotion's,
    and land-wait's changed set names the tags this landing gates. `changed` prints what its
    Pi and setup halves owe on stderr, beside the verdict.
    """
    say(f"deriving tags from the diff since {ln.opts.since}")
    r = ln.tools.deploy_tags(ln.opts.primary, ["changed", ln.opts.since])
    if r.returncode == DEPLOY_BROAD:
        say(
            "`changed` refused the range as broad; the promotion's changed set names its k8s "
            "tags, and the host wait covers a change to a host's package"
        )
        ln.resolved_tags = []
        return
    if r.returncode != DEPLOY_OK:
        ln.die(f"deploy_tags.py changed failed (exit {r.returncode})", 1)
    # An empty element (a doubled or trailing comma) carries no service, so reading it as a
    # tag would end the landing as a tag miss with nothing wrong.
    ln.resolved_tags = [t for t in r.stdout.strip().split(",") if t]


def no_tag_outcome(ln: Landing, scope: str = "no service tag") -> NoReturn:
    """No service tag: owed to a hand, or nothing at all.

    `scope` is why this landing has no tags to deploy, printed on the verdict line. The
    default is the ordinary case — the PR's own files derived none. `derive_from_diff` passes
    its own wording, because a refused broad derivation is not the same fact and an operator
    reading `reaches no service tag` about a 330-file PR goes looking for a different bug.

    A setup role the deployer's timer applies ends `nothing-to-deploy`: this landing reads
    none of the deployer's state, so it reports neither the apply nor a failure of it.
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
