"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no plane a hand applies, no host package and no path that may
move the bundle has nothing to wait for, and CI on the merge commit is not this landing's gate.
The deployer's timer applies a setup role on its own; this landing does not watch it.
"""

import shlex
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Classification, Landing
from deploy_tools.land_lib.fresh import FRESH_FLAG
from deploy_tools.land_lib.own_checkout import OWN_CHECKOUT
from deploy_tools.land_lib.outcome import (
    UNREADABLE_LEDGER_NOTE,
    Outcome,
    Verdict,
    say,
)
from deploy_tools.land_lib import land_tags
from deploy_tools.land_lib.land_tags import DeriveSource
from deploy_tools.land_lib.promotion import moves_the_bundle
from lib.exit_codes import LAND_BAD_ARGS
from lib.narrow_git import CannotNarrow
from lib.repo_paths import GITOPS_DEPLOY_FILES
from renderer.renderer_lib.host_inputs import HOST_INPUTS

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib import deploy_cross_role


def _classified[**P, R](
    ln: Landing, label: str, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run one classification helper bash ran as a subprocess, guarded the way it was.

    bash: `X=$(... land_tags.py ...) || die "<label> failed" 1`. In-process, an unhandled
    exception (a `yaml.YAMLError` reading `host_vars`, say) would otherwise surface as a
    bare traceback instead of `land: <label> failed`. `Outcome` is let through unfiltered:
    a helper that calls `ln.die` itself must not be relabelled as a classification failure.
    """
    try:
        return fn(*args, **kwargs)
    except Outcome:
        raise
    except CannotNarrow as exc:
        ln.die(f"{label} failed: {exc}", 1)
    except Exception:
        ln.die(f"{label} failed", 1)


def resolve(ln: Landing) -> None:
    """Step 1: the merge commit, and a fresh origin/master in the primary checkout."""
    ln.ledger.t_merged = ln.tools.clock()
    sha = (ln.view("mergeCommit").get("mergeCommit") or {}).get("oid") or ""
    if not sha:
        ln.die(f"PR #{ln.opts.pr} has no merge commit — is it merged?", 1)
    ln.merge_sha = sha
    ln.ledger.merge_sha = sha
    say(f"merge commit {sha}")
    ln.fetch_branch()


def _stale_code_reason(ln: Landing, base: str) -> str:
    """Why land.py's checkout lags `base`; "" when it carries every change to its code."""
    stale = ln.tools.stale_code(base)
    if stale == []:
        return ""
    if stale:
        return f"its landing code is older than master's: {', '.join(stale)}"
    return f"git could not compare its landing code with {base}"


def refuse_stale_code_before_merge(ln: Landing) -> None:
    """Die before `await_merge` merges the PR when land.py's checkout lags origin/master.

    `refuse_stale_code` runs after the merge, so on its own a stale checkout's merge wait
    would merge the PR first (#7830). The base is all of master, the PR not yet on it. The
    fetch runs in the primary, and land.py's checkout reads it because it is a worktree of the
    primary and shares its refs.
    """
    ln.fetch_branch()
    base = f"origin/{BRANCH}"
    what = _stale_code_reason(ln, base)
    if not what:
        return
    ln.die(
        f"this landing runs from {OWN_CHECKOUT}, and {what}. Nothing is merged; re-run the "
        f"same land.sh command with `{FRESH_FLAG}`, which lands from a checkout of {base} it "
        "makes and removes itself",
        1,
    )


def refuse_stale_code(ln: Landing) -> None:
    """Die when land.py's checkout lacks a master change to the code this landing runs.

    The base is the merge commit's first parent: master as it stood before this PR. The PR's
    own changes are left out because the landers that ship PRs run from checkouts made before
    the merge: autoland's (`fanout_lib/autoland.py:prepare_checkout`) and the lander unit's,
    the primary. Against the merge itself, every PR editing landing code would refuse there.
    The cost: such a PR that also moves a layout is not caught landing from an older checkout.
    DECIDED: refuse rather than re-exec from a fresh checkout, so `land.sh` keeps running the
    release it ships with (#7820). The refusal names `--fresh`, the caller's opt-in to that
    re-run (#7933), so no hand makes a checkout. A rebase merge of a PR that edits landing
    code puts its earlier commits in the base and refuses too; land.sh squash-merges.
    """
    base = f"{ln.merge_sha}^1"
    what = _stale_code_reason(ln, base)
    if not what:
        return
    # Quoted: zsh's extendedglob reads the `^` in `<sha>^1` as a glob and refuses the line.
    since = shlex.quote(ln.opts.since or base)
    rerun = f"land.sh --pr {ln.opts.pr} --since {since} {FRESH_FLAG}"
    ln.die(
        f"this landing runs from {OWN_CHECKOUT}, and {what}. PR #{ln.opts.pr} is merged, "
        f"and this landing graded nothing; re-run it as `{rerun}`, which lands from a "
        f"checkout of origin/{BRANCH} it makes and removes itself",
        1,
    )


def pr_range(ln: Landing) -> str:
    """`<merge-base>..<pr-head>` from refs/pull/<n>/head, or '' when it cannot be read.

    `--since` covers every other session's merged work, and `MERGE_SHA^` is wrong for a
    rebase merge of a multi-commit PR. The pull ref's merge base
    with the merge commit is the branch point under every merge method. Any step failing
    leaves the range empty, which classifies every broad path as loud -- the direction a
    wrong answer must fall.
    """
    if ln.git("fetch", "-q", "origin", f"refs/pull/{ln.opts.pr}/head").returncode == 0:
        head = ln.git("rev-parse", "FETCH_HEAD")
        if head.returncode == 0 and head.stdout.strip():
            base = ln.git("merge-base", head.stdout.strip(), ln.merge_sha)
            if base.returncode == 0 and base.stdout.strip():
                return f"{base.stdout.strip()}..{head.stdout.strip()}"
    say(
        f"could not read PR #{ln.opts.pr}'s own range — every broad path stays owed to a hand"
    )
    return ""


def _declared_at_merge(ln: Landing) -> set[str] | None:
    """The tags `containers_list` declares at the merge commit, or None when the read failed.

    Asked of the MERGE COMMIT, not of a checkout: a PR that adds a role and its
    `containers_list` entry together is absent from every tree until the tick fast-forwards.
    Every reader falls back to its own tree on None.
    """
    declared = _classified(
        ln, "declared-tag read", ln.tools.declared_at, ln.merge_sha, ln.opts.primary
    )
    if declared is None:
        say(
            f"could not read the k8s service list at {ln.merge_sha[:8]} (is this checkout "
            "older than that commit?) — classifying against this checkout instead"
        )
    return declared


def adopt_cross_role_tables(ln: Landing) -> None:
    """Classify with the merge commit's cross-role tables, not this checkout's (#3976).

    `land_tags` is imported from beside `land.py`, so the tables it reads are whatever that
    checkout holds. PR #3890 added the alert-unit rows and their files together, and a
    landing that read the older copy found no shippers for the files, recorded `common`, and
    printed the resolv.conf remediation for roles the change never reached. The deployer
    reads origin's copy for the same reason (#3512, `deploy_phases`).

    A read or parse failure keeps this checkout's tables and says so. `tables_in` parses
    the copy rather than running it, because this runs before master CI has passed.
    """
    path = deploy_cross_role.CROSS_ROLE_FILE
    shown = ln.git("show", f"{ln.merge_sha}:{path}")
    try:
        if shown.returncode != 0:
            raise OSError(shown.stderr.strip() or f"git show exited {shown.returncode}")
        deploy_cross_role.use_tables(deploy_cross_role.tables_in(shown.stdout))
    except Exception as exc:
        say(
            f"could not read {path} at {ln.merge_sha[:8]} ({exc}) — "
            "classifying with this checkout's cross-role tables"
        )


def _tasks_remain(ln: Landing, role: str) -> bool:
    """Whether setup role `role` still has a task file at the merge commit.

    A tree that cannot be read counts as remaining: the role then stays loud, which is the
    direction a wrong answer must fall.
    """
    prefix = f"ansible/roles/setup/{role}/tasks/"
    listed = ln.git("ls-tree", "-r", "-z", "--full-tree", ln.merge_sha, "--", prefix)
    if listed.returncode != 0:
        return True
    return any(
        record.partition("\t")[2].startswith(prefix)
        for record in listed.stdout.split("\0")
    )


def deletions_owing_nothing(ln: Landing, files) -> frozenset[str]:
    """The paths the PR deletes that no apply is owed for.

    No playbook applies a deleted file. A deleted path in a setup role still counts when the
    role keeps a task file, because the PR may edit another of its files and the deletion
    then changes what the role does (#7906). So a deleted path owes nothing only outside a
    setup role, or in a role whose whole task set the PR deletes.
    """
    kept: dict[str, bool] = {}
    out = set()
    for f in files:
        path = f["path"]
        if f.get("changeType") != "DELETED":
            continue
        roles = land_tags.setup_roles_of(path)
        for role in roles:
            if role not in kept:
                kept[role] = _tasks_remain(ln, role)
        if not any(kept[role] for role in roles):
            out.add(path)
    return frozenset(out)


def owed_manual_planes(
    ln: Landing, paths: list[str], quiet: frozenset[str]
) -> frozenset[str]:
    """The role tags the owed ledger holds as `manual_plane`; an unreadable ledger defers."""
    try:
        return land_tags.owed_for(
            paths, quiet, ln.opts.deployer_state, ln.tools.read_owed
        )
    except land_tags.LedgerUnreadable as exc:
        print(UNREADABLE_LEDGER_NOTE)
        ln.finish(Verdict.DEFERRED, 75, f"PR #{ln.opts.pr}: {exc}")


def classify(ln: Landing) -> None:
    """Tags and the plane a hand must apply."""
    c = ln.classifier
    adopt_cross_role_tables(ln)
    view = ln.view("files,changedFiles")
    files = view.get("files", [])
    paths = [f["path"] for f in files]
    deleted = frozenset() if ln.resolved_tags else deletions_owing_nothing(ln, files)
    recorded = pr_range(ln)
    quiet = c.quiet_paths(paths, recorded)
    # `--tags` named the services, so the plane is left unread: the operator's list wins over
    # a derivation, and `plane` is what a HAND applies rather than an input to any wait this
    # landing takes. Leaving it unread keeps the override's meaning -- deploy exactly these
    # services -- and the broad half a `--tags` landing must not skip is caught earlier, by
    # `ci.preflight`'s blockers read over the incoming range.
    declared = None if ln.resolved_tags else _declared_at_merge(ln)
    owed = (
        frozenset()
        if ln.resolved_tags
        else owed_manual_planes(ln, paths, frozenset(quiet) | deleted)
    )
    plane = (
        ""
        if ln.resolved_tags
        else _classified(
            ln,
            "plane classification",
            c.plane_note,
            paths,
            quiet=frozenset(quiet) | deleted,
            owed=owed,
        )
    )
    ln.classification = Classification(
        pr_paths=tuple(paths),
        pr_range=recorded,
        quiet=frozenset(quiet),
        declared=None if declared is None else frozenset(declared),
        plane=plane,
        deleted=deleted,
        owed=owed,
    )
    if ln.resolved_tags:
        return
    # -1 rather than 0: `gh` omitting the field must not read as agreement with an empty
    # file list, which would silently license a zero-tag deploy.
    tags, source = _classified(
        ln,
        "tag derivation",
        c.derive,
        paths,
        view.get("changedFiles", -1),
        declared,
        quiet=quiet,
    )
    ln.resolved_tags = list(tags)
    if source == DeriveSource.FALLBACK:
        if not ln.opts.since:
            ln.die(
                "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>",
                LAND_BAD_ARGS,
            )
        ln.needs_diff = True
        say(
            f"file list truncated; deriving from the diff since {ln.opts.since} in step 4"
        )


def narrow_plane(ln: Landing) -> None:
    """Render the plane note again with the narrow tags this PR's setup-role change needs.

    Step 1 renders the note with the whole-role tag. The narrow tags come from the PR's own
    derivation, passed in explicitly: every other input is the frozen `Classification`, so
    both renders read the same paths, declared tags and quiet paths.

    Every failure leaves `narrowed_plane` None, so `Landing.plane` stays step 1's note with
    the whole-role tag: no PR range, a derivation that refuses or raises. The render is
    INSIDE the try because a raise here would end `land.py` in a traceback instead of a
    verdict, which is a worse answer than the role tag.
    """
    cl = ln.classification
    if not cl.plane or not cl.pr_range:
        return
    try:
        narrow = ln.tools.own_narrowing(list(cl.pr_paths), cl.pr_range, ln.opts.primary)
        if narrow:
            ln.narrowed_plane = ln.classifier.plane_note(
                list(cl.pr_paths),
                quiet=cl.plane_quiet,
                narrow_tags=narrow,
                owed=cl.owed,
            )
    except Exception as exc:
        say(f"narrowing not read ({type(exc).__name__}) — keeping the role tag")
        return


def reaches_host_inputs(ln: Landing) -> bool:
    """Whether a changed path falls under a member of `HOST_INPUTS`: the PR MAY reach a host.

    It runs no renderer. Resolve reads the merge commit before master CI, and
    `deploy_cross_role.tables_in` records, in `DECIDED parse the copy rather than exec it`, why
    nothing at that point may run the merge commit's code. `host_wait.host_targets` renders
    after CI and says which hosts differ.

    A file list `gh` truncated (`needs_diff`) cannot rule a host input out, so it counts as a
    candidate and the render after CI decides.
    """
    return ln.needs_diff or any(
        path.startswith(HOST_INPUTS) for path in ln.classification.pr_paths
    )


def shortcut_if_nothing(ln: Landing) -> None:
    """A PR reaching no tag, no plane and no host candidate has nothing to wait for.

    A PR whose paths may move the bundle reaches none of them and still waits on a promotion
    (`promotion.moves_the_bundle`), whose changed set names its tags (D6.12). A setup role is such a path, so it waits too.
    """
    cl = ln.classification
    if not (
        ln.resolved_tags
        or ln.plane
        or ln.needs_diff
        or ln.host_candidate
        or moves_the_bundle(cl.pr_paths)
    ):
        ln.finish(
            Verdict.NOTHING_TO_DEPLOY,
            0,
            f"PR #{ln.opts.pr} touched no service",
        )
