"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no rotated secret, no host package and no path that may
move the bundle has nothing to wait for, and CI on the merge commit is not this landing's gate.
A host package applies a setup-plane change, and `host_wait` waits for that host.
"""

import shlex
import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Classification, Landing
from deploy_tools.land_lib.fresh import FRESH_FLAG
from deploy_tools.land_lib.own_checkout import OWN_CHECKOUT
from deploy_tools.land_lib.outcome import Outcome, Verdict, say
from deploy_tools.land_lib.land_tags import DeriveSource
from deploy_tools.land_lib.promotion import moves_the_bundle
from lib.exit_codes import LAND_BAD_ARGS
from lib.k8s_role_callers import CannotNarrow
from renderer.renderer_lib.host_inputs import HOST_INPUTS


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
    rerun = (
        f"re-run the same land.sh command with `{FRESH_FLAG}`, which lands from a checkout "
        f"of {base} it makes and removes itself"
    )
    ln.die(
        f"this landing runs from {OWN_CHECKOUT}, and {what}. Nothing is merged; "
        f"{_unit_rerun(ln) or rerun}",
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
        "and this landing graded nothing; "
        + (
            _unit_rerun(ln)
            or f"re-run it as `{rerun}`, which lands from a checkout of origin/{BRANCH} it "
            "makes and removes itself"
        ),
        1,
    )


def _unit_rerun(ln: Landing) -> str:
    """The re-run a lander unit's caller can make, or "" outside a unit.

    The unit runs a fixed command line and its caller's `land.sh` refuses `--fresh`
    (`handoff.refused_flags`), so the remedy is the next run of the checkout-sync timer.
    """
    if not ln.opts.policy_active:
        return ""
    return (
        f"a lander unit cannot take `{FRESH_FLAG}`, so re-run the same land.sh command once "
        f"{OWN_CHECKOUT} carries origin/{BRANCH}; homelab-checkout-sync fast-forwards the "
        "primary checkout every 10 minutes"
    )


def pr_range(ln: Landing) -> str:
    """`<merge-base>..<pr-head>` from refs/pull/<n>/head, or '' when it cannot be read.

    `--since` covers every other session's merged work, and `MERGE_SHA^` is wrong for a
    rebase merge of a multi-commit PR. The pull ref's merge base
    with the merge commit is the branch point under every merge method. Any step failing
    leaves the range empty, which keeps every comment-only edit loud -- the direction a
    wrong answer must fall.
    """
    if ln.git("fetch", "-q", "origin", f"refs/pull/{ln.opts.pr}/head").returncode == 0:
        head = ln.git("rev-parse", "FETCH_HEAD")
        if head.returncode == 0 and head.stdout.strip():
            base = ln.git("merge-base", head.stdout.strip(), ln.merge_sha)
            if base.returncode == 0 and base.stdout.strip():
                return f"{base.stdout.strip()}..{head.stdout.strip()}"
    say(
        f"could not read PR #{ln.opts.pr}'s own range — no comment-only edit is read as quiet"
    )
    return ""


def _declared_at_merge(ln: Landing) -> set[str] | None:
    """The tags `containers_list` declares at the merge commit, or None when the read failed.

    Asked of the MERGE COMMIT, not of a checkout: a PR that adds a role and its
    `containers_list` entry together is absent from every tree until checkout-sync fast-forwards.
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


def classify(ln: Landing) -> None:
    """Tags and the note a hand must act on."""
    c = ln.classifier
    view = ln.view("files,changedFiles")
    files = view.get("files", [])
    paths = [f["path"] for f in files]
    recorded = pr_range(ln)
    quiet = c.quiet_paths(paths, recorded)
    # `--tags` named the services, so the note is left unread: the operator's list wins over
    # a derivation, and `plane` is what a HAND applies rather than an input to any wait this
    # landing takes.
    declared = None if ln.resolved_tags else _declared_at_merge(ln)
    plane = (
        ""
        if ln.resolved_tags
        else _classified(ln, "plane classification", c.plane_note, paths)
    )
    ln.classification = Classification(
        pr_paths=tuple(paths),
        pr_range=recorded,
        quiet=frozenset(quiet),
        declared=None if declared is None else frozenset(declared),
        plane=plane,
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


def reaches_host_inputs(ln: Landing) -> bool:
    """Whether a changed path falls under a member of `HOST_INPUTS`: the PR MAY reach a host.

    It runs no renderer: resolve reads the merge commit before master CI, and nothing at that
    point may run the merge commit's code. `host_wait.host_targets` renders after CI and says
    which hosts differ.

    A file list `gh` truncated (`needs_diff`) cannot rule a host input out, so it counts as a
    candidate and the render after CI decides.
    """
    return ln.needs_diff or any(
        path.startswith(HOST_INPUTS) for path in ln.classification.pr_paths
    )


def shortcut_if_nothing(ln: Landing) -> None:
    """A PR reaching no tag, no plane and no host candidate has nothing to wait for.

    A PR whose paths may move the bundle reaches none of them and still waits on a promotion
    (`promotion.moves_the_bundle`), whose changed set names its tags (D6.12).
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
