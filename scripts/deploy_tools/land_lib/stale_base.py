"""A head whose base master has moved past: how far it moved, and the one branch update.

A PR's CI run tests the PR merged into the master of its last push, and a merge onto a newer
master makes a tree no run tested (a red master run can follow a PR
whose every job was green). `master_paths_since` asks GitHub which paths master changed since
the master commit the head's green CI run tested it against (the PR's merge base when that
cannot be read).

A landing gives a head one run on the master its merge lands on in two places, both in the
landing turn. A head that has had no CI run gets its branch updated when the landing takes the
turn (`merge._start_run`, `update_branch`). A green head of a ready PR that master moved past
is converted to draft and updated before the landing asks for the turn, so it waits in the
line like any draft (`rejoin_line`). One that master moved past while it waited for the turn is
rejoined once it takes the turn, and gives the turn back (`rejoin_in_turn`), as is one that
master moved past while a rate-limit wait had the turn given up.

A green head that reaches the merge with a stale base anyway merges as it stands when master
changed no path the PR changes, and master's own run tests it. When master did change one,
`refuse_stale_overlap` refuses the merge, whichever route brought the head there: a landing
that went on without the turn marks its draft ready with no update, and that run tests the
master of the PR's last push. An approved head gets a rebase remedy, because an update would
void the approval; any other head gets a re-run, which updates its branch (`rejoin_line`).
"""

import subprocess


from lib.json_types import as_list, as_object, as_object_list
from deploy_tools.land_lib import gh_read, policy
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say
from deploy_tools.land_lib.pr_json import parse_file

# GitHub's compare endpoint lists at most this many files; a list this long may omit a path.
COMPARE_CAP = 300
# Each path the compare lists, and a renamed path's old name as well.
_COMPARE_PATHS = "[.files[]? | .filename, (.previous_filename // empty)]"


def batch_tested(ln: Landing, head: str) -> bool:
    """Whether a landing batch tested the merge of `head` onto the master of this moment.

    `batch.py` records the head it stacked in `ln.tested_head` and the master commit it
    stacked that head on in `ln.tested_on`. While `head` is that head and master is that
    commit, the squash merge makes the tree the batch's run tested, so no rule here has
    anything to judge. Once either has moved, or master cannot be read, the batch's run
    stands for nothing: `ln.tested_on` is cleared, and the rules below judge the head as
    they judge any other.
    """
    if not ln.tested_on:
        return False
    url = f"repos/{{owner}}/{{repo}}/git/ref/heads/{BRANCH}"

    def call() -> str:
        got = ln.tools.gh_json("api", url, "--jq", "{sha: .object.sha}")
        return str(as_object(got or {}, "gh api git/ref").get("sha") or "")

    if head != ln.tested_head:
        why = f"its head is {head[:8]}, not the {ln.tested_head[:8]} the batch stacked"
    else:
        try:
            tip = ln.read(call, "gh api git/ref")
        except (gh_read.GhReadFailed, ValueError) as exc:
            tip = f"unreadable ({exc})"
        if tip == ln.tested_on:
            return True
        why = (
            f"{BRANCH} is {tip[:8] if len(tip) == 40 else tip}, not the "
            f"{ln.tested_on[:8]} the batch stacked it on"
        )
    say(
        f"the batch's run no longer stands for PR #{ln.opts.pr}: {why}; the stale-base "
        "rules judge it"
    )
    ln.tested_on = ""
    return False


def master_paths_since(ln: Landing, head: str) -> list[str]:
    """The paths master changed since the base `head` was last tested against; [] when none did.

    That base is the master commit the head's green `ci.yml` PR run checked out
    (`tools.tested_base`), and the merge base of `head` and master when it cannot be read.

    A renamed path counts under its old name as well as its new one. GitHub's compare endpoint
    lists at most `COMPARE_CAP` files, so a caller reads a capped list as overlap.

    Dies when the read fails: an unknown base is an unknown test result, and the cost is one
    landing to re-run.
    """
    # Only the base moves: a PR pushed after master moved was tested against that newer master. The
    # tested base is from the checkout log's `Merge <head> into <base>`; if unreadable the merge
    # base stands.
    base = ln.tools.tested_base(head) or head
    url = f"repos/{{owner}}/{{repo}}/compare/{base}...{BRANCH}"

    def call() -> list:
        got = ln.tools.gh_json("api", url, "--jq", _COMPARE_PATHS)
        return as_list(got, "gh api compare")

    try:
        paths = ln.read(call, "gh api compare")
    except gh_read.GhReadFailed as exc:
        ln.die(f"could not compare {head[:8]} with {BRANCH}: {exc}", 1)
    except ValueError as exc:
        ln.die(
            f"could not compare {head[:8]} with {BRANCH}: unparseable gh output: {exc}",
            1,
        )
    return [p for p in paths if isinstance(p, str)]


def _shown(paths: list[str]) -> str:
    """The first three of `paths`, and how many more, for a message."""
    more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
    return ", ".join(paths[:3]) + more


def _pr_paths(ln: Landing) -> set[str] | None:
    """Every path the PR's REST `files` listing names, a rename's old path included.

    None when the listing is at GitHub's cap, where a path may be missing. Dies when the read
    fails, as `master_paths_since` does.
    """
    url = f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/files"

    def call() -> list:
        got = ln.tools.gh_json("api", "--paginate", "--slurp", url) or []
        pages = as_list(got, "gh api pulls/files")
        return [parse_file(e) for p in pages for e in as_object_list(p, "files page")]

    try:
        files = ln.read(call, "gh api pulls/files")
    except (gh_read.GhReadFailed, ValueError) as exc:
        ln.die(f"could not list PR #{ln.opts.pr}'s files: {exc}", 1)
    if len(files) >= policy.FILE_CAP:
        return None
    names = [(f["filename"], f.get("previous_filename", "")) for f in files]
    return {p for pair in names for p in pair if p}


def refuse_stale_overlap(ln: Landing, head: str) -> None:
    """Die when master changed a path the PR changes since `head`'s green run tested it.

    Every head is judged, one this landing's own update made included, because master can move
    again during that head's run. A head a landing batch tested is the caller's to exempt
    (`batch_tested`). Overlap the reads cannot rule out counts as overlap: a compare list at
    GitHub's cap, and a file listing at its cap. GitHub's `CONFLICTING` does not count: it
    flickers for one poll whenever master moves, and a real conflict shares a path anyway.
    """
    moved = master_paths_since(ln, head)
    # DECIDED: only a path both master and the PR changed makes a green head stale at the merge
    # Refusing on any master move would refuse nearly every landing, and would make every
    # approved PR be rebased and approved again, since master moves between nearly every
    # approval and its landing. The accepted risk is a red master from a path the PR never
    # touched.
    if moved and len(moved) < COMPARE_CAP:
        pr_paths = _pr_paths(ln)
        if pr_paths is not None:
            moved = sorted(set(moved) & pr_paths)
    if not moved:
        return
    pr = ln.opts.pr
    if head == ln.approved_head:
        # DECIDED: a stale approved head is refused, not updated and not merged. The update
        # would make a head the operator's approval does not name, so the policy would refuse
        # it anyway. The rebase moves the merge base to master, so the re-approved head goes
        # stale only if master moves again between the approval and the landing.
        ln.die(
            f"PR #{pr}'s base is behind {BRANCH} by {len(moved)} changed path(s), "
            "and its approved head cannot be updated without voiding the approval — rebase "
            f"it onto origin/{BRANCH}, push, get the new head approved, and re-run this",
            1,
        )
    shown = _shown(moved)
    ln.die(
        f"{BRANCH} changed {len(moved)} path(s) PR #{pr} changes since the run that tested "
        f"{head[:8]} ({shown}), so merging it would make a tree no run tested — re-run this, "
        f"which updates its branch from {BRANCH} first; a landing that pins its head "
        f"(LAND_REQUIRE_HEAD) needs the branch updated and the new head checked",
        1,
    )


def update_branch(ln: Landing, head: str, moved: list[str]) -> None:
    """Merge master into the PR branch at `head`, so its CI runs against today's master.

    `expected_head_sha` makes GitHub refuse if the branch moved since `head` was read. A
    refusal dies: the PR cannot be re-tested from here, and merging it untested is what this
    check exists to stop.
    """
    shown = _shown(moved)
    say(
        f"{BRANCH} changed {len(moved)} path(s) since PR #{ln.opts.pr}'s base ({shown}); "
        f"updating its branch so CI tests it against {BRANCH} before the merge"
    )
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/update-branch",
            "-f",
            f"expected_head_sha={head}",
        )
    except subprocess.CalledProcessError as exc:
        why = (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
        refuse_conflict(ln, why)
        rebase = f"rebase it onto origin/{BRANCH}, push, and re-run this"
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and GitHub refused to update its "
            f"branch ({why}) — {ln.remedy(why, rebase)}",
            1,
        )
    except subprocess.TimeoutExpired:
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and the branch update timed out — "
            f"rebase it onto origin/{BRANCH}, push, and re-run this",
            1,
        )


# GitHub applies a branch update asynchronously, usually within seconds; wait this long before
# marking the PR ready anyway.
NEW_HEAD_WAIT_S = 60
NEW_HEAD_POLL_S = 3


def update_for_run(ln: Landing, head: str, draft: bool) -> bool:
    """Update the PR's branch at `head` from master, for the run its turn is about to start.

    For a draft, waits up to `NEW_HEAD_WAIT_S` for GitHub to show the new head, so the ready
    flip after it starts the run there. Any refusal but a conflict leaves the PR the run it
    has.

    Returns:
        Whether the branch was updated.

    Raises:
        Outcome: `merge-conflict`, when GitHub refuses the update as a conflict.
    """
    try:
        moved = master_paths_since(ln, head)
        if not moved:
            return False
        update_branch(ln, head, moved)
    except Outcome as exc:
        if exc.verdict == Verdict.MERGE_CONFLICT:
            raise
        say(f"{exc.error}; going on with the run the PR has")
        return False
    waited = 0
    while draft and waited < NEW_HEAD_WAIT_S:
        if (ln.view("state,mergeable,headRefOid").get("headRefOid") or head) != head:
            break
        ln.tools.sleep(NEW_HEAD_POLL_S)
        waited += NEW_HEAD_POLL_S
    return True


def _why(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    return (getattr(exc, "stderr", None) or str(exc)).strip()


# What gh prints when GitHub refuses a branch update because the head conflicts with master.
CONFLICT_REFUSAL = "merge conflict between base and head"


def refuse_conflict(ln: Landing, why: str) -> None:
    """End the landing `merge-conflict` when `why`, a refused branch update, is a conflict.

    GitHub's merge follows no rename, so a PR that edits a file master moved conflicts here
    although a local `git merge` of the same two commits is clean.
    """
    if CONFLICT_REFUSAL in why:
        ln.die(
            f"PR #{ln.opts.pr} conflicts with {BRANCH}: GitHub refused to update its branch "
            f"({why}) — rebase it, push, then re-run this",
            1,
            Verdict.MERGE_CONFLICT,
        )


def ready_again(ln: Landing) -> None:
    """Mark the PR's draft ready; die when GitHub refuses, because a draft never merges."""
    pr = str(ln.opts.pr)
    try:
        ln.tools.gh("pr", "ready", pr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as again:
        why = _why(again)
        remedy = ln.remedy(why, f"run `gh pr ready {pr}`, then re-run this")
        ln.die(
            f"PR #{pr} is a draft and could not be marked ready ({why}); {remedy}", 1
        )


def rejoin_line(ln: Landing, head: str, conflicting: bool) -> bool:
    """Put a green ready head that master moved past back in the line for the turn.

    A PR opened ready got its run on the master of its last push. Merged as it stands, it
    makes a tree no run tested, and master's own run sweeps in full: 16 of 26 master runs on
    2026-11-14 did (`docs/ci-throughput.md`). Converted to draft and updated from master, it
    has a head whose only run skipped, so it stands in the line, a holder of the turn can
    stack it, and its own turn gives it one run on the master its merge lands on.

    Leaves alone a head the update would void or that it cannot make: one GitHub reads as
    `conflicting`, one the operator approved, and one `LAND_REQUIRE_HEAD` names. An update
    that fails marks the PR ready again, so the landing goes on as it would have; dies when
    that fails too, because a draft never merges.

    Returns:
        Whether the PR is now a draft at an updated head, for `merge._start_run` to take
        once GitHub has moved the head.
    """
    if conflicting or head == ln.approved_head or ln.opts.require_head is not None:
        return False
    try:
        moved = master_paths_since(ln, head)
    except Outcome as exc:
        say(f"{exc.error}; landing it as it stands")
        return False
    if not moved:
        return False
    pr = str(ln.opts.pr)
    try:
        ln.tools.gh("pr", "ready", "--undo", pr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        say(
            f"could not convert PR #{pr} to draft ({_why(exc)}); landing it as it stands"
        )
        return False
    try:
        ln.tools.gh(
            "api",
            "-X",
            "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{pr}/update-branch",
            "-f",
            f"expected_head_sha={head}",
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        say(f"could not update PR #{pr}'s branch ({_why(exc)}); marking it ready again")
        ready_again(ln)
        refuse_conflict(ln, _why(exc))
        return False
    say(
        f"{BRANCH} changed {len(moved)} path(s) since the master PR #{pr}'s run tested; "
        "converted it to draft and updated its branch, so it waits in the line for a run "
        f"on the {BRANCH} of its turn"
    )
    return True


def rejoin_in_turn(ln: Landing, head: str, conflicting: bool) -> bool:
    """Give the landing turn back and rejoin the line when master moved past `head`.

    For a landing that holds the turn and finds master moved after the run of `head`: while
    it waited in line, or while a rate-limit wait had given the turn up
    (`Landing.without_turn`).

    Returns:
        Whether the PR is now a draft at an updated head and the turn is given back; False,
        changing nothing, for a landing that does not hold the turn or a head `rejoin_line`
        leaves alone.
    """
    if not (ln.give_back_turn and rejoin_line(ln, head, conflicting)):
        return False
    ln.give_back_turn()
    say("gave the landing turn back to wait in the line as a draft")
    return True
