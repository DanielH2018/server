"""Step 5's other half: a k8s tag lands through `promote`, never through deploy.sh.

Flux applies every k8s `containers_list` entry once `promote` moves `prod`, and deploy.sh refuses
such a tag with exit 5 (`deploy_lib/owners.py`). `split_k8s` therefore takes every such tag out of
`resolved_tags` before `deploy.deploy_by_host` sees the list. A tag `--tags` named is kept for the
wait, and a tag this PR's paths named is dropped: every landing whose PR `moves_the_bundle` waits
on the promotion, and land-wait's changed set, read from the two `digests.json` files, names the
tags the health gate and the `source=promote` marker read (`WaitResult.tags`). A PR that moves no
bundle file waits on `homelab` alone and gates no tag. The plan is
docs/target-architecture/deploy/d6-delete-old-path.md, *`land.sh`'s k8s tags after D6*.

`wait_on_promotion` runs `promote.py land-wait` on the merge commit. Its outcomes map to a
landing's words:

| land-wait | Verdict | Cause | Exit |
|---|---|---|---|
| settled | the landing goes on to the health gate | -- | -- |
| deploy-failed | `deploy-failed` | `promote-held`, or `soak-failed` for R's `flux/soak` | 1 |
| gave-up | `deferred` | `promote-timeout` | 75 |
| suspended | `nothing-to-deploy`, or the next steps when deploy.sh deployed a tag, a host package changed or the tick applies part of the PR | -- | 0 |
| suspended, with a plane or other hosts owed | `needs-manual-apply` | -- | 1 |

`gave-up` keeps the closed verdict set: it prints `deferred` beside `PROMOTE_TIMEOUT_NOTE`, and
`land_probe` reads the note and reports the landing as `gave-up`, a resume point, rather than as
the ordinary deferral that clears on its own.

`suspended` means every published tag of the PR is suspended at the merge commit, so Flux
applies nothing for them until a PR resumes them (D5.1d). A settled wait may name suspended
tags too, when only some were. Either way the gate leaves the suspended tags out.
"""

import subprocess

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import (
    PROMOTE_TIMEOUT_NOTE,
    Cause,
    Verdict,
    host_not_waited_note,
    remaining_hosts_note,
    say,
)
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib.deploy_changes import _is_test_only_path, is_doc


def _reaches_no_render(path: str) -> bool:
    """A document or a test, which no render reads; a `.md` a role ships under `files/` may."""
    if _is_test_only_path(path):
        return True
    return is_doc(path) and not (
        path.startswith("ansible/roles/") and "/files/" in path
    )


def moves_the_bundle(paths) -> bool:
    """Whether a changed path may move the bundle `promote` publishes (D6.12).

    Every path but a document or a test may: the landing waits on land-wait, whose changed
    set says what moved, and a PR that moved nothing waits on `homelab` alone. A prefix list
    of render inputs would end a PR it missed at `nothing-to-deploy` unwaited.
    """
    return not all(_reaches_no_render(p) for p in paths)


def gated_tags(ln: Landing) -> list[str]:
    """The tags step 7 gates: those deploy.sh deployed, then those a promotion applied.

    A tag suspended at the merge commit is left out, as Flux applied nothing for it.
    """
    return ln.resolved_tags + [t for t in ln.flux_tags if t not in ln.suspended_tags]


# What `FluxReads.owners_at` raises when the ownership map cannot be read.
OWNER_READ_ERRORS = (ValueError, subprocess.SubprocessError, OSError)


def split_k8s(ln: Landing) -> None:
    """Take every k8s tag out of `resolved_tags`; a `--tags` one joins `flux_tags` for the wait.

    The k8s entries are read at the merge commit, the tree this landing deploys, because the
    primary checkout may not carry the PR yet. An unreadable list ends the landing: reading it
    as no k8s tag would hand each tag to deploy.sh, which refuses it.

    A path-derived tag is a guess at what the render moves. A `tasks/`-only change moves no
    bundle file, and a shared template moves tags no path names, so the landing waits on
    land-wait's changed set instead and gates what it read.
    """
    if not ln.resolved_tags:
        return
    try:
        declared = ln.tools.flux.k8s_tags_at(ln.opts.primary, ln.merge_sha)
    except OWNER_READ_ERRORS as exc:
        ln.die(
            f"could not read which tags Flux applies at {ln.merge_sha[:12]}, so nothing "
            f"was deployed: {exc}",
            1,
        )
    k8s = [tag for tag in ln.resolved_tags if tag in declared]
    if not k8s:
        return
    ln.resolved_tags = [tag for tag in ln.resolved_tags if tag not in declared]
    ln.k8s_only = [tag for tag in ln.k8s_only if tag not in declared]
    if ln.opts.tags:
        ln.flux_tags = k8s
        for tag in k8s:
            say(f"{tag}: a k8s entry, so it lands through promote, not deploy.sh")
        return
    say(
        f"{','.join(k8s)}: named by this PR's paths; the promotion's changed set names the "
        "tags this landing gates"
    )


def _flux_owned(ln: Landing, tags) -> list[str]:
    """The `owner: flux` tags at the merge commit among `tags`, which the gate may ask about.

    A widened changed set names every Kustomization the cluster lists, so this keeps a name
    that is no deploy tag away from `probe.py health`. An unreadable ownership map keeps
    every name and says so: a gate asked too much fails loudly, one asked too little passes.
    """
    if not tags:
        return []
    try:
        owners = ln.tools.flux.owners_at(ln.opts.primary, ln.merge_sha)
    except OWNER_READ_ERRORS as exc:
        say(
            f"could not read which tags Flux applies at {ln.merge_sha[:12]} ({exc}); gating every changed name"
        )
        return list(tags)
    return [tag for tag in tags if owners.get(tag) == "flux"]


# What a landing keeps back from `LAND_DEADLINE` after the promotion wait: the health gate and
# the verdict and ledger lines written after it.
DEADLINE_RESERVE_S = 600


def wait_cap(ln: Landing) -> float | None:
    """The seconds the promotion wait may take before `LAND_DEADLINE`, or None with none set."""
    if not ln.opts.deadline:
        return None
    spent = ln.tools.clock() - ln.ledger.t_start
    return max(0.0, ln.opts.deadline - spent - DEADLINE_RESERVE_S)


def _print_what_else_it_owes(ln: Landing) -> None:
    """The notes a landing ended by this wait never reaches: the health step and the host wait."""
    if ln.plane:
        print(f"  it needs applying by hand: {ln.plane}")
    if ln.classification.remaining_setup:
        print(remaining_hosts_note(ln.classification.remaining_setup))
    for line in ln.tick_half_open_lines():
        print(line)
    if ln.host_targets:
        print(host_not_waited_note(ln.host_targets))


def wait_on_promotion(ln: Landing) -> None:
    """Wait until a promotion covers the merge commit; return on `settled`, else end the landing.

    The changed set's tags join `flux_tags`, so the health gate and the ledger read what the
    promotion applied rather than what the PR's paths named.
    """
    pr, sha = ln.opts.pr, ln.merge_sha
    named = f" ({','.join(ln.flux_tags)})" if ln.flux_tags else ""
    say(f"waiting until a promotion covers {sha[:12]}{named}")
    result = ln.tools.flux.land_wait(
        sha, list(ln.flux_tags), ln.opts.primary, wait_cap(ln), ln.opts.window_grace
    )
    ln.flux_tags += [t for t in _flux_owned(ln, result.tags) if t not in ln.flux_tags]
    # Before any `finish` below, so a failed landing's ledger line names the changed set.
    ln.ledger.tags_label = ",".join(ln.resolved_tags + ln.flux_tags)
    tags = ",".join(ln.flux_tags) or "homelab"
    if result.tags:
        say(f"the changed set names {','.join(result.tags)}")
    if result.widened:
        say(f"land-wait waited on every Kustomization: {result.widened}")
    if result.suspended:
        ln.suspended_tags = list(result.suspended)
        say(
            f"suspended at {sha[:12]}, so neither waited on nor gated: "
            + ", ".join(result.suspended)
        )
    if result.verdict == "settled":
        say(f"promoted: {result.detail}")
        return
    print(f"  {result.detail}")
    # DECIDED: `suspended`, every published tag suspended, ends the landing as
    # `nothing-to-deploy`, exit 0, unless deploy.sh deployed a tag, which the gate still
    # grades, a host package changed, which the host wait still waits on, the tick applies part
    # of the PR, whose state `no_tag_outcome` and the health step grade, or a plane or another
    # host is owed a hand, which ends `needs-manual-apply` as in `no_tag_outcome`. The suspend
    # PR itself owes none of these and ends at exit 0, so a failure verdict would be wrong for
    # it. Exit 75 would be wrong too: `land_probe` reads 75 as a resume point, and a re-run
    # waits on the same suspended tag again. A change that raced the gate onto a suspended tag
    # never ends here: land-wait leaves it to `promote.py decide`'s `suspended-changed` hold.
    if result.verdict == "suspended":
        if ln.resolved_tags:
            return
        owes_a_hand = ln.plane or ln.classification.remaining_setup
        if (ln.host_targets or ln.classification.self_applied) and not owes_a_hand:
            return
        if owes_a_hand:
            _print_what_else_it_owes(ln)
            ln.finish(
                Verdict.NEEDS_MANUAL_APPLY,
                1,
                f"PR #{pr}, {sha} — {result.detail}. The landing gated none of {tags}, "
                "and what it owes a hand is above",
            )
        ln.finish(
            Verdict.NOTHING_TO_DEPLOY,
            0,
            f"PR #{pr}, {sha} — {result.detail}. The landing gated none of {tags}",
        )
    if result.verdict != "suspended":
        _print_what_else_it_owes(ln)
    if result.verdict == "deploy-failed" and result.cause == Cause.SOAK_FAILED:
        ln.ledger.cause = Cause.SOAK_FAILED
        ln.finish(
            Verdict.DEPLOY_FAILED,
            1,
            f"PR #{pr}, {sha} — the soak failed on {result.revision[:12]}, so promote's "
            f"verify holds it and {tags} is not verified",
        )
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
