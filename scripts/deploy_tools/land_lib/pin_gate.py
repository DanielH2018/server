"""The pin gate: what `land.sh` checks before it merges a forward-only pin bump (H1.1g).

After H1, merging `k3s_longhorn_version` or `k3s_metallb_version` is the upgrade: Flux applies
it on promotion, and neither upgrade runs backwards. So `merge.await_merge` calls
`gate_before_merge` on the head it is about to merge, and a PR that moves neither pin passes
through with no read beyond its two pin lookups. For a PR that moves one, in this order:

1. **The host.** Anywhere but daniel-box refuses, because gate 2 of `longhorn-upgrade` reads
   the restore drill's stamp from daniel-box's disk (failure class 7).
2. **The land rule.** `DEPLOY_HOLD` must read exactly `none`; empty, missing, a SHA and a
   failed read all refuse. Then (`last-good`, master tip] must hold no commit that already
   changes the same pin. `last-good`'s commit comes from `promote.py`'s own reader, and a read
   that fails refuses. Two bumps of one pin in flight would apply as one hop, which for
   Longhorn is a skipped minor.
3. **The gates.** `runbook_gates.py longhorn-upgrade` or `metallb-upgrade`, as a subprocess.
   A non-zero exit refuses.
4. **The status.** `homelab/upgrade-gates`, state `success`, on the PR's head SHA, with the
   gate summary and who ran it in the description. H1.1b's `promote` requires it for every
   pin-changing commit it applies. A failed post refuses the merge.

A merge GitHub refuses is retried at the same head. Steps 3 and 4 run once per head, and
`recheck_before_merge` reads step 2 again before every retry.

`regate` (`land.sh --regate <pr>`) runs steps 1, 3 and 4 for a PR that is already MERGED, and
never merges. It skips the land rule on purpose: the merged bump is itself in (`last-good`,
tip], and the procedure that clears a hold runs it while the hold is set.

What this covers, and what it cannot. It runs only in `land.sh --arm-merge`'s own merge. A
merge from the GitHub UI, a bare `gh pr merge` or an auto-merge bypasses it; H1.1b's
backstop in `promote` holds such a merge until an operator runs `--regate` and clears the
hold. The plan is docs/target-architecture/host/h1-cluster-config-to-flux.md, *At most one
unverified bump per pin* and *Longhorn and MetalLB bumps gate at the merge*.
"""

import getpass
import subprocess
from collections.abc import Mapping

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `artifact_retention` are imported bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from validate.cluster_pins import Pin, Unjudged

import forge
from artifact_retention import LAST_GOOD_TAG
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say
from deploy_tools.land_lib.pin_reads import Unreadable

# Each forward-only pin, and the `runbook_gates.py` set its bump runs.
GATE_SETS: Mapping[str, str] = {
    "k3s_longhorn_version": "longhorn-upgrade",
    "k3s_metallb_version": "metallb-upgrade",
}
STATUS_CONTEXT = "homelab/upgrade-gates"
GATE_HOST = "daniel-box"
NO_HOLD = "none"
# GitHub refuses a commit status whose description is longer than this.
DESCRIPTION_MAX = 140


def _value(pins: Mapping[str, Pin | None], key: str) -> str | None:
    pin = pins.get(key)
    return pin.value if pin is not None else None


def changed_pins(
    base: Mapping[str, Pin | None], head: Mapping[str, Pin | None]
) -> list[str]:
    """The forward-only pins whose value differs between `base` and `head`, in GATE_SETS order.

    A key that moves to another file at the same value is no change, as in `cluster_pins`.
    """
    return [k for k in GATE_SETS if _value(base, k) != _value(head, k)]


def hold_problem(value: str | None) -> str:
    """Why DEPLOY_HOLD's value refuses a pin bump, or "" when it reads `none`."""
    if value == NO_HOLD:
        return ""
    if value is None:
        return f"{forge.HOLD_VARIABLE} does not exist, which reads as an error, never as no hold"
    if not value.strip():
        return (
            f"{forge.HOLD_VARIABLE} is empty, which reads as an error, never as no hold"
        )
    return f"{forge.HOLD_VARIABLE} holds {value!r}"


def changes_in_range(
    values: list[tuple[str, str | None]],
) -> list[str]:
    """Each commit in `values` whose pin value differs from the one before it.

    Args:
        values: `(sha, value)` pairs, oldest first; the first is the range's lower bound and
            is never reported itself.
    """
    return [
        sha
        for (_, before), (sha, after) in zip(values, values[1:], strict=False)
        if before != after
    ]


def description(gate_set: str, output: str, who: str) -> str:
    """The status description: who ran which set, and the runner's last line, at most 140 chars."""
    last = output.strip().splitlines()[-1] if output.strip() else "no output"
    text = f"{who}: {gate_set}: {last}"
    return text if len(text) <= DESCRIPTION_MAX else text[: DESCRIPTION_MAX - 1] + "…"


def _who(ln: Landing) -> str:
    return f"land.sh as {getpass.getuser()}@{ln.tools.hostname().split('.')[0]}"


def _merge_base(ln: Landing, head: str) -> str:
    """Fetch the PR's head and master, then their merge base, or die."""
    pr = ln.opts.pr
    fetched = ln.git(
        "fetch", "-q", "--no-write-fetch-head", "origin", f"refs/pull/{pr}/head", BRANCH
    )
    if fetched.returncode != 0:
        ln.die(f"could not fetch PR #{pr}'s head and {BRANCH} for the pin gate", 1)
    base = ln.git("merge-base", f"origin/{BRANCH}", head)
    if base.returncode != 0 or not base.stdout.strip():
        ln.die(
            f"could not find PR #{pr}'s merge base with {BRANCH}, so whether it bumps a "
            "forward-only pin is unknown — not merging",
            1,
        )
    return base.stdout.strip()


def _pins(ln: Landing, sha: str) -> Mapping[str, Pin | None]:
    try:
        return ln.tools.flux.pins.pins_at(ln.opts.primary, sha)
    except (Unjudged, subprocess.CalledProcessError) as exc:
        ln.die(f"could not read the forward-only pins at {sha[:9]}: {exc}", 1)


def bumped_pins(ln: Landing, head: str) -> list[str]:
    """The forward-only pins PR `ln.opts.pr` moves at `head`, read in the primary checkout."""
    base = _merge_base(ln, head)
    return changed_pins(_pins(ln, base), _pins(ln, head))


def _require_gate_host(ln: Landing, pins: list[str]) -> None:
    host = ln.tools.hostname().split(".")[0]
    if host != GATE_HOST:
        ln.die(
            f"PR #{ln.opts.pr} bumps {' and '.join(pins)}, whose gates read the restore "
            f"drill's stamp on {GATE_HOST}'s disk, and this is {host} — run land.sh on "
            f"{GATE_HOST}",
            1,
        )


def land_rule(ln: Landing, pins: list[str]) -> None:
    """Die unless DEPLOY_HOLD reads `none` and (`last-good`, tip] changes none of `pins`."""
    pr = ln.opts.pr
    try:
        hold = ln.tools.flux.pins.hold()
    except Unreadable as exc:
        ln.die(
            f"could not read {forge.HOLD_VARIABLE}, so PR #{pr}'s bump waits: {exc}", 1
        )
    if problem := hold_problem(hold):
        ln.die(
            f"PR #{pr} bumps {' and '.join(pins)} and {problem}. A bump merges only while "
            f"{forge.HOLD_VARIABLE} reads `{NO_HOLD}`; clearing a hold is the operator's",
            1,
        )
    try:
        last_good = ln.tools.flux.pins.last_good()
    except Unreadable as exc:
        ln.die(
            f"could not read `{LAST_GOOD_TAG}`'s commit, so PR #{pr} waits: {exc}", 1
        )
    # DECIDED: an absent `last-good` refuses rather than falling back to `prod` or to "no
    # range". `promote` seeds the tag on its first run, so its absence means the promotion
    # plane is not running yet, and a bump merged then has nothing that would hold it.
    if last_good is None:
        ln.die(
            f"`{LAST_GOOD_TAG}` does not exist, so whether a bump of "
            f"{' and '.join(pins)} is already in flight is unknown — not merging",
            1,
        )
    tip = ln.git("rev-parse", f"origin/{BRANCH}")
    if tip.returncode != 0:
        ln.die(f"could not resolve origin/{BRANCH} for the land rule", 1)
    # First-parent with ancestry-path, as `promote.py`'s own walk reads (`last-good`, T]. A
    # `last-good` that is not on master's first-parent line leaves the range empty, so it
    # is refused rather than read as "no change in flight".
    ancestor = ln.git("merge-base", "--is-ancestor", last_good, tip.stdout.strip())
    if ancestor.returncode != 0:
        ln.die(
            f"`{LAST_GOOD_TAG}`'s commit {last_good[:9]} is not an ancestor of "
            f"origin/{BRANCH}, so the range the land rule reads is unknown — not merging",
            1,
        )
    walk = ln.git(
        "rev-list",
        "--reverse",
        "--first-parent",
        "--ancestry-path",
        f"{last_good}..{tip.stdout.strip()}",
    )
    if walk.returncode != 0:
        ln.die(f"could not walk {last_good[:9]}..origin/{BRANCH} for the land rule", 1)
    shas = [last_good, *walk.stdout.split()]
    at = {sha: _pins(ln, sha) for sha in shas}
    for key in pins:
        if hits := changes_in_range([(sha, _value(at[sha], key)) for sha in shas]):
            ln.die(
                f"PR #{pr} bumps {key}, and (`{LAST_GOOD_TAG}`, {BRANCH} tip] already "
                f"changes it at {', '.join(h[:9] for h in hits)}. One unverified bump per "
                "pin: wait until promote verifies that one and moves last-good past it",
                1,
            )
    say(
        f"land rule: {forge.HOLD_VARIABLE} reads `{NO_HOLD}`, and "
        f"({LAST_GOOD_TAG} {last_good[:9]}, {BRANCH} tip] changes no pin this PR bumps"
    )


def _gate_and_post(ln: Landing, pins: list[str], head: str) -> None:
    """Run each pin's gate set, then post `homelab/upgrade-gates` on `head`, or die."""
    pr = ln.opts.pr
    summaries = []
    for key in pins:
        gate_set = GATE_SETS[key]
        rc, output = ln.tools.flux.pins.run_gates(gate_set)
        print(output)
        if rc != 0:
            ln.die(
                f"PR #{pr} bumps {key}, and `runbook_gates.py {gate_set}` exited {rc} — "
                "fix what it names and re-run this",
                1,
            )
        summaries.append((gate_set, output))
    text = description(" + ".join(s for s, _ in summaries), summaries[-1][1], _who(ln))
    try:
        ln.tools.gh(
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/statuses/{head}",
            "-f",
            "state=success",
            "-f",
            f"context={STATUS_CONTEXT}",
            "-f",
            f"description={text}",
        )
    except subprocess.CalledProcessError as exc:
        ln.die(
            f"the gates passed but posting {STATUS_CONTEXT} on {head[:9]} failed, and "
            f"promote holds a bump without it — not merging: {(exc.stderr or '').strip()}",
            1,
        )
    except subprocess.TimeoutExpired:
        ln.die(f"posting {STATUS_CONTEXT} on {head[:9]} timed out — not merging", 1)
    say(f"posted {STATUS_CONTEXT}=success on {head[:9]}: {text}")


def gate_before_merge(ln: Landing, head: str) -> list[str]:
    """The pins `head` bumps, once it may merge; die when it bumps one it may not.

    The caller passes the list to `recheck_before_merge` before every later merge attempt
    at the same head.
    """
    pins = bumped_pins(ln, head)
    if not pins:
        return []
    say(f"PR #{ln.opts.pr} bumps {' and '.join(pins)}; running the pin gate")
    _require_gate_host(ln, pins)
    land_rule(ln, pins)
    _gate_and_post(ln, pins, head)
    return pins


def recheck_before_merge(ln: Landing, pins: list[str]) -> None:
    """Re-read DEPLOY_HOLD and (`last-good`, tip] before a retried merge at a gated head.

    A merge GitHub refuses is retried at the same head for up to `merge_timeout`. In that
    time a failed promotion can set DEPLOY_HOLD, or another session can merge a bump of the
    same pin, so the land rule is read again before each attempt. The gates and the status
    stay once per head: the status is what promote checks, and its age bound covers the wait.
    """
    if not pins:
        return
    if ln.git("fetch", "-q", "origin", BRANCH).returncode != 0:
        ln.die(f"could not fetch {BRANCH} to re-check the land rule — not merging", 1)
    land_rule(ln, pins)


def regate(ln: Landing) -> None:
    """Re-run the gates for a merged PR and re-post its status; die on any refusal.

    Never merges, deploys or reads DEPLOY_HOLD.
    """
    pr = ln.opts.pr
    view = ln.view("state,headRefOid")
    if view.get("state") != "MERGED":
        ln.die(
            f"PR #{pr} is {view.get('state') or 'in an unknown state'}, and --regate is "
            "for a merged PR; land an open one with --arm-merge --await-merge",
            1,
        )
    head = view.get("headRefOid") or ""
    if not head:
        ln.die(f"PR #{pr} has no head SHA to post {STATUS_CONTEXT} on", 1)
    pins = bumped_pins(ln, head)
    if not pins:
        ln.die(f"PR #{pr} bumps no forward-only pin, so it has no gate to re-run", 1)
    _require_gate_host(ln, pins)
    _gate_and_post(ln, pins, head)
