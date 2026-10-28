"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no plane a hand applies and nothing the tick applies
itself has nothing to wait for: the deployer fast-forwards it on its own tick, and CI on
the merge commit is the deployer's gate, not this landing's.
"""

import sys as _sys
from collections.abc import Callable
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Classification, Landing
from deploy_tools.land_lib.outcome import Outcome, Verdict, say
from deploy_tools.land_lib.land_tags import DeriveSource
from deploy_tools.land_lib.promotion import moves_the_bundle
from lib.exit_codes import LAND_BAD_ARGS
from lib.repo_paths import GITOPS_DEPLOY_FILES
from renderer.renderer_lib.host_inputs import HOST_INPUTS

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from deployer_lib import deploy_cross_role
from deployer_lib.deploy_narrow import NARROWED_TO_NOTHING


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
    except Exception:
        ln.die(f"{label} failed", 1)


def resolve(ln: Landing) -> None:
    """Step 1: the merge commit, and a fresh origin/master in the primary checkout."""
    ln.ledger.t_merged = ln.tools.clock()
    sha = (ln.view("mergeCommit").get("mergeCommit") or {}).get("oid") or ""
    if not sha:
        ln.die(f"PR #{ln.opts.pr} has no merge commit — is it merged?", 1)
    ln.merge_sha = sha
    ln.ledger.merged_at = ln.tools.wall_clock()
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


def _owed_to_a_hand(
    ln: Landing, paths: list[str], recorded: str, quiet: set[str]
) -> tuple[set[str] | None, list[str], str]:
    """The tags declared at the merge commit, and the plane note over the paths a hand owes.

    Returns `(declared, plane_paths, plane)`: `declared` is None when the read failed.
    """
    # WHICH TAGS EXIST is asked of the MERGE COMMIT, not of a checkout. A PR that adds a role
    # and its `containers_list` entry together is absent from every tree until the tick
    # fast-forwards, so a checkout answers "this role is unregistered" — the same thing it says
    # about a role somebody forgot to register, and `needs-manual-apply` then prints the
    # expensive remedy (a full `ansible/deploy.yml`) for a role one `--tags` run deploys.
    # None means the read failed, and every reader
    # below falls back to its own tree.
    declared = _classified(
        ln, "declared-tag read", ln.tools.declared_at, ln.merge_sha, ln.opts.primary
    )
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
    plane_paths = _classified(
        ln,
        "deploy-time-only shared-role classification",
        ln.tools.paths_a_hand_must_apply,
        paths,
        recorded,
        ln.opts.primary,
        declared,
    )
    if len(plane_paths) != len(paths):
        say(
            "no rendered manifest reads what this PR changed under "
            f"{','.join(sorted(set(paths) - set(plane_paths)))}, so it takes effect on "
            "the next deploy rather than needing one"
        )
    plane = _classified(
        ln,
        "plane classification",
        ln.classifier.plane_note,
        plane_paths,
        declared,
        quiet=quiet,
    )
    return declared, plane_paths, plane


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
    adopt_cross_role_tables(ln)
    view = ln.view("files,changedFiles")
    paths = [f["path"] for f in view.get("files", [])]
    recorded = pr_range(ln)
    quiet = c.quiet_paths(paths, recorded)
    self_applied = _classified(
        ln, "self-applied classification", c.self_applied, paths, quiet=quiet
    )
    # The command a hand runs if the tick turns out NOT to have applied its own half —
    # derived over the same paths `self_applied` reads, so the two cannot name different work.
    self_applied_command = _classified(
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
    remaining_setup = _classified(
        ln,
        "remaining-setup-hosts classification",
        c.remaining_setup_hosts,
        paths,
        t.hostname(),
        quiet=quiet,
        pr_range=recorded,
        ref=ln.merge_sha,
        repo=ln.opts.primary,
    )
    # `--tags` named the services, so the plane is left unread: the operator's list wins over
    # a derivation, and `plane` is what a HAND applies rather than an input to any wait this
    # landing takes. Leaving it unread keeps the override's meaning -- deploy exactly these
    # services -- and the broad half a `--tags` landing must not skip is caught earlier, by
    # `ci.preflight`'s blockers read over the incoming range.
    declared, plane_paths, plane = (
        (None, paths, "")
        if ln.resolved_tags
        else _owed_to_a_hand(ln, paths, recorded, quiet)
    )
    ln.classification = Classification(
        pr_paths=tuple(paths),
        pr_range=recorded,
        quiet=frozenset(quiet),
        declared=None if declared is None else frozenset(declared),
        plane_paths=tuple(plane_paths),
        plane=plane,
        self_applied=self_applied,
        self_applied_command=self_applied_command,
        remaining_setup=remaining_setup,
    )
    if ln.resolved_tags:
        # DECIDED: `--tags` leaves `k8s_only` empty, so a two-platform tag such as `wg-easy`
        # routes to every declaring host (#2748). The operator's list carries no provenance,
        # and narrowing it from the PR's paths would second-guess an explicit "deploy exactly
        # these services". Staying wide costs one Pi Compose deploy that recreates nothing.
        # Narrowing is the direction of issue #929: a tag that reached no host while the
        # landing printed `settled` over a Pi still running the old container.
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
    if source == DeriveSource.PR:
        # Which of these tags a changed PATH proves is a k3s change. Read over `paths`
        # rather than `tags`, so that a tag `derive` reached some other way than by path stays
        # unproven rather than being credited to a tree. The FALLBACK path
        # answers the same question in step 5 instead, over the diff's paths rather than this
        # file list, which `gh` truncated -- `deploy.record_k8s_only`.
        ln.k8s_only = _classified(
            ln, "k8s-only tag classification", c.k8s_only_tags, paths, declared
        )
        # DECIDED: no shared-role caller joins the list (D6.12). Every caller of a shared k8s
        # role is flux-owned, so the promotion applies it, and land-wait's changed set names
        # each caller whose render moved. `land_shared.shared_caller_tags` goes in D6.3.
    if source == DeriveSource.FALLBACK:
        if not ln.opts.since:
            ln.die(
                "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>",
                LAND_BAD_ARGS,
            )
        # The diff derivation reads `<since>...HEAD` in the primary checkout, which the tick
        # has not fast-forwarded yet. Derived in step 5, after the tick.
        ln.needs_diff = True
        say(
            f"file list truncated; deriving from the diff since {ln.opts.since} after the tick"
        )


def narrow_plane(ln: Landing, awaited: bool = True) -> None:
    """Render the plane note again with the narrow tags this PR's setup-role change needs.

    Step 1 renders the note before the tick has crossed this PR's range, so it names the
    whole-role tag. The narrow tags are the one input that does not exist yet, and it is
    passed in explicitly: every other input is the frozen `Classification`, so both renders
    read the same paths, declared tags and quiet paths. After an `awaited` tick the tags come
    from the deployer's receipt for the tick that crossed the merge commit (#3391), whose
    `manual` half holds the narrowest tags each role needs over that tick's range alone. The
    range contains this PR, so the row is quoted as it stands.

    A landing that deploys its own merge commit never awaits the tick (`awaited=False`), so
    no receipt exists for its range. It renders the PR's own derivation instead.

    Every failure leaves `narrowed_plane` None, so `Landing.plane` stays step 1's note with
    the whole-role tag: no receipt (a deployer that has not shipped the writer, or a tick
    that recorded nothing), a role the receipt could not narrow, no PR range on the fast
    path, a derivation that refuses or raises. The render is INSIDE the try because a raise
    here would end `land.py` in a traceback instead of a verdict, which is a worse answer
    than the role tag.
    """
    cl = ln.classification
    if not cl.plane or (not awaited and not cl.pr_range):
        return
    try:
        if awaited:
            receipt = ln.receipt_for(ln.merge_sha)
            # `narrowed-to-nothing` is the tick's answer that the range needs nothing from
            # the role, and it recorded no `manual_plane` line for it (#4408). The empty set
            # is how `plane_note` takes that answer. An empty row is a refusal and is dropped,
            # so the note keeps the whole-role tag.
            narrow = {
                t: frozenset() if tags == {NARROWED_TO_NOTHING} else tags
                for t, tags in (receipt.manual if receipt else {}).items()
                if tags
            }
        else:
            narrow = ln.tools.own_narrowing(
                list(cl.pr_paths), cl.pr_range, ln.opts.primary
            )
        if narrow:
            ln.narrowed_plane = ln.classifier.plane_note(
                list(cl.plane_paths),
                cl.declared_set(),
                quiet=cl.quiet,
                narrow_tags=narrow,
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
    """A PR reaching no tag, no plane, no host candidate and nothing self-applied has nothing to wait for.

    A PR whose paths may move the bundle reaches none of them and still waits on a promotion
    (`promotion.moves_the_bundle`), whose changed set names its tags (D6.12).
    """
    cl = ln.classification
    if not (
        ln.resolved_tags
        or ln.plane
        or cl.self_applied
        or ln.needs_diff
        or ln.host_candidate
        or moves_the_bundle(cl.pr_paths)
    ):
        ln.finish(
            Verdict.NOTHING_TO_DEPLOY,
            0,
            f"PR #{ln.opts.pr} touched no service; the deployer fast-forwards it on its next tick",
        )
