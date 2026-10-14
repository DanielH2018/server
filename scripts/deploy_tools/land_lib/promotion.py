"""Step 5's other half: a tag Flux applies lands through `promote`, never through deploy.sh.

A k8s `containers_list` entry that sets `owner` is not Ansible's to deploy
(`deploy_lib/owners.py`). `owner: flux` is published, so Flux applies it once `promote` moves
`prod`. `owner: released` is mid-handback, so nothing applies it. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*.

`split_owned` takes every such tag out of `resolved_tags` into `owned_tags` before
`deploy.deploy_by_host` sees the list, because `deploy_by_host` hands its list to deploy.sh as
given and deploy.sh refuses such a tag with exit 5. A PR whose only tags are such tags runs no
deploy.sh at all.

`wait_on_promotion` then runs `promote.py land-wait` on the merge commit. Its three outcomes
map to a landing's words:

| land-wait | Verdict | Cause | Exit |
|---|---|---|---|
| settled | the landing goes on to the health gate | -- | -- |
| deploy-failed | `deploy-failed` | `promote-held` | 1 |
| gave-up | `deferred` | `promote-timeout` | 75 |

`gave-up` keeps the closed verdict set: it prints `deferred` beside `PROMOTE_TIMEOUT_NOTE`, and
`land_probe` reads the note and reports the landing as `gave-up`, a resume point, rather than as
the ordinary deferral that clears on its own.
"""

import subprocess

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import PROMOTE_TIMEOUT_NOTE, Cause, Verdict, say


def published(ln: Landing) -> list[str]:
    """The `owner: flux` tags `split_owned` took, which a promotion applies and step 6 gates."""
    return [tag for tag, owner in ln.owned_tags.items() if owner == "flux"]


def gated_tags(ln: Landing) -> list[str]:
    """The tags step 6 gates: those deploy.sh deployed, then those a promotion applied."""
    return ln.resolved_tags + published(ln)


def split_owned(ln: Landing) -> None:
    """Move every tag that is not Ansible-owned out of `resolved_tags` into `owned_tags`.

    Ownership is read at the merge commit, the tree this landing deploys, because the primary
    checkout may not carry the PR yet. An unreadable ownership map ends the landing: reading an
    unknown `owner` as Ansible-owned would hand the tag to two writers.
    """
    if not ln.resolved_tags:
        return
    try:
        owners = ln.tools.flux.owners_at(ln.opts.primary, ln.merge_sha)
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        ln.die(
            f"could not read which tags Flux applies at {ln.merge_sha[:12]}, so nothing "
            f"was deployed: {exc}",
            1,
        )
    ln.owned_tags = {tag: owners[tag] for tag in ln.resolved_tags if tag in owners}
    if not ln.owned_tags:
        return
    ln.resolved_tags = [tag for tag in ln.resolved_tags if tag not in owners]
    ln.k8s_only = [tag for tag in ln.k8s_only if tag not in owners]
    for tag, owner in ln.owned_tags.items():
        say(f"{tag}: owner: {owner}, so it lands through promote, not deploy.sh")


def wait_on_promotion(ln: Landing) -> None:
    """Wait until a promotion covers the merge commit; return on `settled`, else end the landing."""
    pr, sha = ln.opts.pr, ln.merge_sha
    tags = ",".join(ln.owned_tags)
    say(f"waiting until a promotion covers {sha[:12]} ({tags})")
    result = ln.tools.flux.land_wait(sha, published(ln), ln.opts.primary)
    if result.widened:
        say(f"land-wait waited on every Kustomization: {result.widened}")
    if result.verdict == "settled":
        say(f"promoted: {result.detail}")
        return
    print(f"  {result.detail}")
    if result.verdict == "deploy-failed":
        ln.ledger.cause = Cause.PROMOTE_HELD
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr}, {sha} — DEPLOY_HOLD holds promotion, so {tags} is not applied",
        )
    print(PROMOTE_TIMEOUT_NOTE)
    ln.ledger.cause = Cause.PROMOTE_TIMEOUT
    ln.finish(
        Verdict.DEFERRED,
        75,
        f"PR #{pr}, {sha} — no promotion covered it in time; {tags} not yet applied",
    )
