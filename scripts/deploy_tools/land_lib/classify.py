"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no plane a hand applies and nothing the tick applies
itself has nothing to wait for: the deployer fast-forwards it on its own tick, and CI on
the merge commit is the deployer's gate, not this landing's.
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
    # the others.
    ln.remaining_setup = _classified(
        ln,
        "remaining-setup-hosts classification",
        c.remaining_setup_hosts,
        paths,
        t.hostname(),
        quiet=quiet,
    )
    ln.plane_paths = paths
    if ln.resolved_tags:
        # `--tags` named the services, so nothing below runs: the operator's list wins over a
        # derivation, and `plane` is what a HAND applies rather than an input to any wait this
        # landing takes. Leaving it unread keeps the override's meaning -- deploy exactly these
        # services -- and the broad half a `--tags` landing must not skip is caught earlier, by
        # `ci.preflight`'s blockers read over the incoming range.
        #
        # DECIDED: `--tags` leaves `k8s_only` empty, so a two-platform tag such as `wg-easy`
        # routes to every declaring host (#2748). The operator's list carries no provenance,
        # and narrowing it from the PR's paths would second-guess an explicit "deploy exactly
        # these services". Staying wide costs one Pi Compose deploy that recreates nothing.
        # Narrowing is the direction of issue #929: a tag that reached no host while the
        # landing printed `settled` over a Pi still running the old container.
        return
    # WHICH TAGS EXIST is asked of the MERGE COMMIT, not of a checkout. A PR that adds a role
    # and its `containers_list` entry together is absent from every tree until the tick
    # fast-forwards, so a checkout answers "this role is unregistered" — the same thing it says
    # about a role somebody forgot to register, and `needs-manual-apply` then prints the
    # expensive remedy (a full `ansible/deploy.yml`) for a role one `--tags` run deploys.
    # None means the read failed, and every reader
    # below falls back to its own tree.
    declared = _classified(
        ln, "declared-tag read", t.declared_at, ln.merge_sha, ln.opts.primary
    )
    ln.declared = declared
    if declared is None:
        say(
            f"could not read containers_list at {ln.merge_sha[:8]} — "
            "classifying against this checkout instead"
        )
    # A shared role whose change reaches no rendered manifest is live for the next deploy of
    # any caller, so no hand applies it: `manifests_rollout_timeout_default` is read as a
    # `--timeout` while a deploy runs, so a 20-minute
    # `ansible/deploy.yml` for it would change nothing. Its paths come out of the
    # list the note is built from; every failure inside returns the list unchanged.
    ln.plane_paths = _classified(
        ln,
        "deploy-time-only shared-role classification",
        t.paths_a_hand_must_apply,
        paths,
        ln.pr_range,
        ln.opts.primary,
        declared,
    )
    if len(ln.plane_paths) != len(paths):
        say(
            "no rendered manifest reads what this PR changed under "
            f"{','.join(sorted(set(paths) - set(ln.plane_paths)))}, so it takes effect on "
            "the next deploy rather than needing one"
        )
    ln.plane = _classified(
        ln, "plane classification", c.plane_note, ln.plane_paths, declared, quiet=quiet
    )
    # -1 rather than 0: `gh` omitting the field must not read as agreement with an empty
    # file list, which would silently license a zero-tag deploy.
    tags, source = _classified(
        ln, "tag derivation", c.derive, paths, view.get("changedFiles", -1), declared
    )
    ln.resolved_tags = list(tags)
    if source == DeriveSource.PR:
        # Which of these tags a changed PATH proves is a k3s change. Read over `paths`
        # rather than `tags`, so that a tag `derive` reached some other way than by path stays
        # unproven rather than being credited to a tree. The FALLBACK path
        # answers the same question in step 5 instead, over the diff's paths rather than this
        # file list, which `gh` truncated -- `deploy.record_k8s_only`.
        ln.k8s_only = _classified(
            ln, "k8s-only tag classification", c.k8s_only_tags, paths, declared
        )
        # A shared role deploys through every role that runs it, so `plane_note` does not
        # name one. Read over `plane_paths`, the list that note read, so a change
        # `shared_role_reach` found deploy-time only fans out to nothing.
        reached = _classified(
            ln,
            "shared-role caller expansion",
            c.shared_caller_tags,
            ln.plane_paths,
            declared,
        )
        smoke = _classified(
            ln,
            "shared-role smoke narrowing",
            c.smoke_narrowed_roles,
            ln.plane_paths,
            declared,
        )
        for role, role_tags in sorted(reached.items()):
            if role in smoke:
                # The operator line must not claim the fleet: this change moves no rendered
                # byte, so one caller proves the task logic runs and the rest pick it up on
                # their own next deploy (#3124).
                say(
                    f"`{role}` renders no different bytes for this change; deploying "
                    f"{', '.join(sorted(role_tags))} as a smoke test, and its other callers "
                    "take the new task logic on their own next deploy"
                )
            elif role_tags:
                say(
                    f"`{role}` has no deploy tag of its own; deploying the "
                    f"{len(role_tags)} service(s) that run it"
                )
        extra = set().union(*reached.values()) - set(tags)
        if extra:
            # The caller graph is the provenance, so these ARE proven k3s.
            ln.k8s_only = sorted(set(ln.k8s_only) | extra)
            ln.resolved_tags = sorted(set(tags) | extra)
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


def narrow_plane(ln: Landing, awaited: bool = True) -> None:
    """Re-render `plane` with the narrow tags this PR's setup-role change needs.

    `plane` is classified in step 1, before the tick has recorded this PR's range, so it
    names the whole-role tag. The deployer then records the narrowest tags in its
    `manual_plane_tags` sidecar. After an `awaited` tick this reads that sidecar, and
    `land_tags.confirmed_narrow_tags` quotes a row only where it contains this PR's own
    derivation, so a stale row from an earlier range cannot be printed.

    A landing that deploys its own merge commit never awaits the tick (`awaited=False`), so
    no row exists for its range. It prints the PR's own derivation instead, so the note names
    the narrow tags the change needs rather than the whole-role tag.

    Every failure keeps the note as step 1 wrote it, with the whole-role tag: an unreadable
    or absent sidecar, no PR range, a derivation that refuses or raises. The re-render is
    INSIDE the try because a raise here, though unlikely (step 1 already called `plane_note`
    on these inputs), would end `land.py` in a traceback instead of a verdict, which is a
    worse answer than the role tag.
    """
    if not ln.plane or not ln.pr_range:
        return
    sidecar = None if not awaited else ln.state("manual_plane_tags")
    if awaited and not sidecar:
        return
    try:
        if awaited:
            narrow = ln.tools.confirm_narrowing(
                ln.pr_paths, ln.pr_range, ln.opts.primary, sidecar
            )
        else:
            narrow = ln.tools.own_narrowing(ln.pr_paths, ln.pr_range, ln.opts.primary)
        if narrow:
            ln.plane = ln.classifier.plane_note(
                ln.plane_paths, ln.declared, quiet=ln.quiet, narrow_tags=narrow
            )
    except Exception as exc:
        say(f"narrowing not read ({type(exc).__name__}) — keeping the role tag")
        return


def shortcut_if_nothing(ln: Landing) -> None:
    """A PR reaching no tag, no plane and nothing self-applied has nothing to wait for."""
    if not (ln.resolved_tags or ln.plane or ln.self_applied or ln.needs_diff):
        ln.finish(
            Verdict.NOTHING_TO_DEPLOY,
            0,
            f"PR #{ln.opts.pr} touched no service; the deployer fast-forwards it on its next tick",
        )
