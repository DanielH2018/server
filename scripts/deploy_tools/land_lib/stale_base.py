"""A green head whose base master has moved past: what makes it stale, and the one update.

--await-merge re-tests a PR whose base is stale before it merges it (issue #4013). Merge
queues are not offered on a user-owned repo and no ruleset requires an up-to-date branch, so
nothing else re-tests a PR against the master it merges into. 4 of 5 sampled red master runs
followed a PR whose every job was green, and two of them came from a master commit merged
after the PR's base. Once the head reads green, `master_paths_since` asks GitHub which paths
master changed since the master commit the head's green CI run tested it against (the PR's
merge base when that cannot be read). When one of them is a path the PR changes, or GitHub
reads the PR as conflicting with master, `update_branch` merges master into the PR branch,
and the merge waits for CI on the updated head instead. A PR whose files master did not touch
merges as it stands (#6565). A landing updates the branch at most once. It never updates the branch of a head the landing policy admitted on
an approval, which the update would void: a stale approved head is refused with a rebase
remedy instead.

A landing that holds the landing turn makes the same update earlier, for a head that has had
no CI run, so that its one run tests the master of the turn (`merge._start_run`). A landing
that takes turns puts a green head of a ready PR that master moved past back in the line as a
draft instead (`rejoin_line`), so `stale_paths` judges only a head that could not rejoin.
"""

import subprocess
from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import as_list, as_object, as_object_list
from deploy_tools.land_lib import gh_read, policy
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Outcome, say
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
            tip = gh_read.read(call, ln.tools.sleep, "gh api git/ref")
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
    lists at most `COMPARE_CAP` files, so `stale_paths` reads a capped list as stale.

    Dies when the read fails: an unknown base is an unknown test result, and the cost is one
    landing to re-run.
    """
    # Only the base moves: a PR pushed after master moved was tested against that newer master,
    # so the merge base alone would update a branch CI had already tested in full (149 of 235
    # landings in 2026-10-15..17, 642 CI minutes, #5487). The tested base is the master commit
    # in the checkout log's `Merge <head> into <base>`; when it cannot be read the merge base
    # stands, which costs one CI run at worst.
    base = ln.tools.tested_base(head) or head
    url = f"repos/{{owner}}/{{repo}}/compare/{base}...{BRANCH}"

    def call() -> list:
        got = ln.tools.gh_json("api", url, "--jq", _COMPARE_PATHS)
        return as_list(got, "gh api compare")

    try:
        paths = gh_read.read(call, ln.tools.sleep, "gh api compare")
    except gh_read.GhReadFailed as exc:
        ln.die(f"could not compare {head[:8]} with {BRANCH}: {exc}", 1)
    except ValueError as exc:
        ln.die(
            f"could not compare {head[:8]} with {BRANCH}: unparseable gh output: {exc}",
            1,
        )
    return [p for p in paths if isinstance(p, str)]


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
        files = gh_read.read(call, ln.tools.sleep, "gh api pulls/files")
    except (gh_read.GhReadFailed, ValueError) as exc:
        ln.die(f"could not list PR #{ln.opts.pr}'s files: {exc}", 1)
    if len(files) >= policy.FILE_CAP:
        return None
    names = [(f["filename"], f.get("previous_filename", "")) for f in files]
    return {p for pair in names for p in pair if p}


def stale_paths(
    ln: Landing, head: str, conflicting: bool, note: Callable[[str], None]
) -> list[str]:
    """The paths that make `head` stale enough to update; [] to merge it as it stands.

    `note` takes the line that says master moved only on paths the PR leaves alone; the wait
    passes an `OnChange`, so a merge attempt repeated on the same master does not repeat it.

    `head` is stale when master changed a path the PR changes since its tested base, or when
    `conflicting` says GitHub reads the PR as conflicting with master. Dies instead when `head`
    is one the operator approved and is stale.
    """
    moved = master_paths_since(ln, head)
    # DECIDED: only a path both master and the PR changed makes a green head stale (operator
    # ruling 2026-10-29, #6565). The update never cancelled an in-flight run; its cost was a
    # second full CI run, about 22% of ci.yml runner-minutes. The accepted risk is the shape of
    # #2922 and #2266, which went red on master from a path the PR never touched. A conflict,
    # a capped compare list and a capped file listing still update, since overlap is unknown.
    if moved and not conflicting and len(moved) < COMPARE_CAP:
        pr_paths = _pr_paths(ln)
        if pr_paths is not None:
            overlap = sorted(set(moved) & pr_paths)
            if not overlap:
                note(
                    f"{BRANCH} changed {len(moved)} path(s) since PR #{ln.opts.pr}'s base, "
                    "none of them one the PR changes; merging without a branch update"
                )
            moved = overlap
    if moved and head == ln.approved_head:
        # DECIDED: a stale approved head is refused, not updated and not merged. The update
        # would make a head the operator's approval does not name, so the policy would refuse
        # it anyway, and merging it untested is what this check exists to stop. The rebase
        # moves the merge base to master, so the re-approved head goes stale only if master
        # moves again between the approval and the landing.
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} by {len(moved)} changed path(s), "
            "and its approved head cannot be updated without voiding the approval — rebase "
            f"it onto origin/{BRANCH}, push, get the new head approved, and re-run this",
            1,
        )
    return moved


def update_branch(ln: Landing, head: str, moved: list[str]) -> None:
    """Merge master into the PR branch at `head`, so its CI runs against today's master.

    `expected_head_sha` makes GitHub refuse if the branch moved since `head` was read. A
    refusal dies: the PR cannot be re-tested from here, and merging it untested is what this
    check exists to stop.
    """
    shown = ", ".join(moved[:3]) + (
        f" and {len(moved) - 3} more" if len(moved) > 3 else ""
    )
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
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and GitHub refused to update its "
            f"branch ({why}) — rebase it onto origin/{BRANCH}, push, and re-run this",
            1,
        )
    except subprocess.TimeoutExpired:
        ln.die(
            f"PR #{ln.opts.pr}'s base is behind {BRANCH} and the branch update timed out — "
            f"rebase it onto origin/{BRANCH}, push, and re-run this",
            1,
        )


def _why(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    return (getattr(exc, "stderr", None) or str(exc)).strip()


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
    if conflicting or head == ln.approved_head or ln.opts.require_head:
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
        try:
            ln.tools.gh("pr", "ready", pr)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as again:
            ln.die(
                f"PR #{pr} is a draft and could not be marked ready ({_why(again)}); run "
                f"`gh pr ready {pr}`, then re-run this",
                1,
            )
        return False
    say(
        f"{BRANCH} changed {len(moved)} path(s) since the master PR #{pr}'s run tested; "
        "converted it to draft and updated its branch, so it waits in the line for a run "
        f"on the {BRANCH} of its turn"
    )
    return True
