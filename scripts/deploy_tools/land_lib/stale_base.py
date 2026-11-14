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
no CI run, so that its one run tests the master of the turn (`merge._start_run`).
"""

import subprocess
from collections.abc import Callable

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.json_types import as_list, as_object_list
from deploy_tools.land_lib import gh_read, policy
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say
from deploy_tools.land_lib.pr_json import parse_file

# GitHub's compare endpoint lists at most this many files; a list this long may omit a path.
COMPARE_CAP = 300
# Each path the compare lists, and a renamed path's old name as well.
_COMPARE_PATHS = "[.files[]? | .filename, (.previous_filename // empty)]"


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
