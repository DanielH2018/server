"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no plane a hand applies and nothing the tick applies
itself has nothing to wait for: the deployer fast-forwards it on its own tick, and CI on
the merge commit is the deployer's gate, not this landing's. Sixteen of the 45 landings
before 2026-09-02 ended nothing-to-deploy after a median seven minutes of PR CI plus
master CI.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path
from typing import Any

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say
from deploy_tools.land_tags import DeriveSource


def _classified(
    ln: Landing, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
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


def pr_range(ln: Landing) -> str:
    """`<merge-base>..<pr-head>` from refs/pull/<n>/head, or '' when it cannot be read.

    `--since` covers every other session's merged work, and `MERGE_SHA^` is wrong for a
    rebase merge of a multi-commit PR (PR #843 was two commits). The pull ref's merge base
    with the merge commit is the branch point under every merge method. Any step failing
    leaves the range empty, which classifies every broad path as loud -- the direction a
    wrong answer must fall (issue #848).
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


def classify(ln: Landing) -> None:
    """Tags, the plane a hand must apply, and whether the tick applies part of this PR.

    Computed whether or not tags were derived: a PR can touch a deployable role AND a
    plane, and then the deploy succeeds while half the change is unapplied.

    `--tags` skips the DERIVATION only. The self-applied half is classified on that path too,
    because `tick_is_the_apply` decides whether step 4 awaits the tick and an override that
    left it False landed a mixed PR on the fast path: the deploy succeeded, the tick's own
    half was never graded, and the landing printed `settled` over an unapplied setup role.
    `--tags` is the form CLAUDE.md prescribes for scoping to your own services, so it is the
    common way to land, not an escape hatch.
    """
    t, c = ln.tools, ln.classifier
    view = ln.view("files,changedFiles")
    paths = [f["path"] for f in view.get("files", [])]
    ln.pr_paths, ln.pr_range = paths, pr_range(ln)
    quiet = c.quiet_paths(paths, ln.pr_range)
    ln.quiet = set(quiet)
    ln.self_applied = _classified(
        ln, "self-applied classification", c.self_applied, paths, quiet=quiet
    )
    # The command a hand runs if the tick turns out NOT to have applied its own half —
    # derived over the same paths `self_applied` reads, so the two cannot name different work.
    ln.self_applied_command = _classified(
        ln,
        "self-applied-command classification",
        c.self_applied_command,
        paths,
        quiet=quiet,
    )
    # What a self-applied setup role still needs beyond the host the tick runs on.
    # initial_setup.yml applies to ONE target per run, so a role with no `when:` gate reaches
    # every host the playbook is ever run on, and the tick converging here says nothing about
    # the others (issue #1009, PR #1002: two hosts kept the old kuma-push-lib.sh for three
    # days behind a `settled` verdict).
    ln.remaining_setup = _classified(
        ln,
        "remaining-setup-hosts classification",
        c.remaining_setup_hosts,
        paths,
        t.hostname(),
        quiet=quiet,
    )
    if ln.resolved_tags:
        # `--tags` named the services, so nothing below runs: the operator's list wins over a
        # derivation, and `plane` is what a HAND applies rather than an input to any wait this
        # landing takes. Leaving it unread keeps the override's meaning -- deploy exactly these
        # services -- and the broad half a `--tags` landing must not skip is caught earlier, by
        # `ci.preflight`'s blockers read over the incoming range.
        return
    # WHICH TAGS EXIST is asked of the MERGE COMMIT, not of a checkout. A PR that adds a role
    # and its `containers_list` entry together is absent from every tree until the tick
    # fast-forwards, so a checkout answers "this role is unregistered" — the same thing it says
    # about a role somebody forgot to register, and `needs-manual-apply` then prints the
    # expensive remedy (a full `ansible/deploy.yml`) for a role one `--tags` run deploys.
    # PR #1539 landed that way (issue #1544). None means the read failed, and every reader
    # below falls back to its own tree exactly as it did before.
    declared = _classified(
        ln, "declared-tag read", t.declared_at, ln.merge_sha, ln.opts.primary
    )
    ln.declared = declared
    if declared is None:
        say(
            f"could not read containers_list at {ln.merge_sha[:8]} — "
            "classifying against this checkout instead"
        )
    ln.plane = _classified(
        ln, "plane classification", c.plane_note, paths, declared, quiet=quiet
    )
    # -1 rather than 0: `gh` omitting the field must not read as agreement with an empty
    # file list, which would silently license a zero-tag deploy.
    tags, source = _classified(
        ln, "tag derivation", c.derive, paths, view.get("changedFiles", -1), declared
    )
    ln.resolved_tags = list(tags)
    if source == DeriveSource.FALLBACK:
        if not ln.opts.since:
            ln.die(
                "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>"
            )
        # The diff derivation reads `<since>...HEAD` in the primary checkout, which the tick
        # has not fast-forwarded yet. Derived in step 5, after the tick.
        ln.needs_diff = True
        say(
            f"file list truncated; deriving from the diff since {ln.opts.since} after the tick"
        )


def narrow_plane(ln: Landing) -> None:
    """Re-render `plane` with the deployer's narrow tags, once the tick has run (#2307).

    `plane` is classified in step 1, before the tick has recorded this PR's range, so it
    names the whole-role tag. The deployer then records the narrowest tags in its
    `manual_plane_tags` sidecar. This reads that sidecar AFTER the awaited tick.
    `land_tags.confirmed_narrow_tags` quotes a row only where it contains this PR's own
    derivation, so a stale row from an earlier range cannot be printed (#2324 review,
    finding 1).

    Every failure keeps the note as step 1 wrote it, with the whole-role tag: an unreadable
    or absent sidecar, no PR range, a derivation that refuses or raises.
    """
    if not ln.plane or not ln.pr_range:
        return
    sidecar = ln.state("manual_plane_tags")
    if not sidecar:
        return
    try:
        narrow = ln.tools.confirm_narrowing(
            ln.pr_paths, ln.pr_range, ln.opts.primary, sidecar
        )
    except Exception as exc:
        say(f"narrowing not read ({type(exc).__name__}) — keeping the role tag")
        return
    if narrow:
        ln.plane = ln.classifier.plane_note(
            ln.pr_paths, ln.declared, quiet=ln.quiet, narrow_tags=narrow
        )


def shortcut_if_nothing(ln: Landing) -> None:
    """A PR reaching no tag, no plane and nothing self-applied has nothing to wait for."""
    if not (ln.resolved_tags or ln.plane or ln.self_applied or ln.needs_diff):
        ln.finish(
            Verdict.NOTHING_TO_DEPLOY,
            0,
            f"PR #{ln.opts.pr} touched no service; the deployer fast-forwards it on its next tick",
        )
