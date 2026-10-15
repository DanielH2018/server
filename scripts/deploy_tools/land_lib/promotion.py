"""Step 5's other half: a tag Flux applies lands through `promote`, never through deploy.sh.

A k8s `containers_list` entry that sets `owner` is not Ansible's to deploy
(`deploy_lib/owners.py`). `owner: flux` is published, so Flux applies it once `promote` moves
`prod`. `owner: released` is mid-handback, so nothing applies it. The plan is
docs/target-architecture/deploy/d2-first-leaf.md, *How `land.sh` waits*.

`split_owned` takes every such tag out of `resolved_tags` into `owned_tags` before
`deploy.deploy_by_host` sees the list, because `deploy_by_host` hands its list to deploy.sh as
given and deploy.sh refuses such a tag with exit 5. A PR whose only tags are such tags runs no
deploy.sh at all.

`owned_by_narrowing` finds such a tag where the PR's derivation cannot. A deploy-plane edit,
such as the `host_vars` line that flips an entry to `owner: flux`, classifies as broad: the
tick applies it and derives no tag for it. The deployer's own narrowing still names the tag,
and the tick drops it because Flux applies it, so the landing reads the same narrowing and
waits on promotion for it (#5146).

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
from lib.exit_codes import DEPLOY_BROAD, DEPLOY_OK
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_changes import _BROAD_DEPLOY_PREFIXES


def published(ln: Landing) -> list[str]:
    """The `owner: flux` tags `split_owned` took, which a promotion applies and step 6 gates."""
    return [tag for tag, owner in ln.owned_tags.items() if owner == "flux"]


def gated_tags(ln: Landing) -> list[str]:
    """The tags step 6 gates: those deploy.sh deployed, then those a promotion applied."""
    return ln.resolved_tags + published(ln)


def owners_at_merge(ln: Landing) -> dict[str, str]:
    """Each tag that is not Ansible-owned at the merge commit, to its `owner`; dies unread.

    Ownership is read at the merge commit, the tree this landing deploys, because the primary
    checkout may not carry the PR yet. An unreadable ownership map ends the landing: reading an
    unknown `owner` as Ansible-owned would hand the tag to two writers.
    """
    try:
        return ln.tools.flux.owners_at(ln.opts.primary, ln.merge_sha)
    except (ValueError, subprocess.SubprocessError, OSError) as exc:
        ln.die(
            f"could not read which tags Flux applies at {ln.merge_sha[:12]}, so nothing "
            f"was deployed: {exc}",
            1,
        )


def split_owned(ln: Landing) -> None:
    """Move every tag that is not Ansible-owned out of `resolved_tags` into `owned_tags`."""
    if not ln.resolved_tags:
        return
    owners = owners_at_merge(ln)
    ln.owned_tags = {tag: owners[tag] for tag in ln.resolved_tags if tag in owners}
    if not ln.owned_tags:
        return
    ln.resolved_tags = [tag for tag in ln.resolved_tags if tag not in owners]
    ln.k8s_only = [tag for tag in ln.k8s_only if tag not in owners]
    for tag, owner in ln.owned_tags.items():
        say(f"{tag}: owner: {owner}, so it lands through promote, not deploy.sh")


def owned_by_narrowing(ln: Landing) -> None:
    """Add to `owned_tags` each tag the deployer's narrowing of a deploy-plane PR names.

    The deployer narrows a deploy-plane range to the tags its render reaches, then drops each
    tag that is not Ansible-owned (`deploy_narrow._deploy_plane`). The PR's own derivation
    never names those tags, because the edit is broad, so without this a landing graded the
    tick alone and printed `settled` while the promotion was still running.

    `deploy_tags.py narrow` runs over the PR's own range, the commits the PR added, rather
    than `--since`, which also holds other sessions' merges. A tag it names that is still
    Ansible-owned stays with the tick, which applied it; only the rest enter `owned_tags`.
    A refusal (exit 3) still prints the tags it reached, and those are read too: a bulk flip
    covering most of the fleet is refused, and its tags need the wait as much as one tag
    does. No PR range, no deploy-plane path, or any other failure adds nothing, which is the
    landing as it was before #5146.
    """
    cl = ln.classification
    if not any(p.startswith(_BROAD_DEPLOY_PREFIXES) for p in cl.pr_paths):
        return
    if not cl.pr_range:
        say("no PR range to narrow, so no tag Flux applies is looked for")
        return
    base, head = cl.pr_range.split("..", 1)
    r = ln.tools.deploy_tags(ln.opts.primary, ["narrow", base, head])
    if r.returncode not in (DEPLOY_OK, DEPLOY_BROAD):
        say(
            f"the deployer's narrowing failed on this PR's range (exit {r.returncode}), so "
            "no tag Flux applies is looked for"
        )
        return
    narrowed = [t for t in r.stdout.strip().split(",") if t]
    if not narrowed:
        return
    owners = owners_at_merge(ln)
    found = {
        tag: owners[tag]
        for tag in narrowed
        if tag in owners and tag not in ln.owned_tags
    }
    ln.owned_tags |= found
    for tag, owner in found.items():
        say(
            f"{tag}: this PR's deploy-plane change reaches it, and owner: {owner}, so it "
            "lands through promote"
        )


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
