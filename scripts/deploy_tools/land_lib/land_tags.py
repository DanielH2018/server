#!/usr/bin/env python3
"""Derive deploy tags from a merged PR's own file list.

WHY NOT A SHA RANGE. `deploy.sh --changed <ref>` diffs a commit range, and after a merge
that range covers every other session's merged work too. Deploying somebody else's
half-finished landing is not this session's to do. A PR's file list is exactly this
session's scope.

WHY A ROLE NAME IS NOT AUTOMATICALLY A TAG. Only a role with a `containers_list` entry has
one. Eight roles under ansible/roles/k8s/ have no entry because other roles include them by
literal name, and handing one to `--tags` makes deploy.sh refuse the WHOLE list (exit 2) --
so the valid services beside it are refused too. Those roles come out of the tags, and
`shared_caller_tags` puts the tags of every role that runs them back in.

WHY THE COUNT ASSERTION. `gh pr view --json files` paginates at 100. A 137-file PR returns
100 entries with no error and no marker, so the derived tag list is a silent subset of what
merged -- and every downstream check reads green over it. When the returned count disagrees
with the PR's own `changedFiles`, this reports `fallback` and the caller widens to
`deploy.sh --changed <since>`: wider than the truth is recoverable, narrower is not.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags.py
"""

import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


# The build/roll couplings live in deploy_logic so this and `deploy_tags.py changed` widen
# identically -- two derivations that disagree is the defect this import exists to prevent.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))  # scripts/deploy_tools

# `service_tags_at` is re-exported rather than defined here: `narrow_broad` needs it too,
# and importing this module for it would put a cycle through `deploy_tags`.
from lib.cli_help import answer_help
from lib.exit_codes import USAGE_ERROR
from lib.render_guard import (  # noqa: F401
    containers_entries_in,
    landing_hosts_for_tags,
    service_records_at_or_none,
    service_tags_at,
)
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES, REPO

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))


sys.path.insert(0, str(HOST_LIB_FILES))
from deployer_lib.deploy_logic import (
    broad_remediation,
    k8s_remediation,
    setup_role_playbook,
    manual_plane_clear_for,
    setup_role_tag,
    tick_applies_setup_role,
)

# `deploy_tags.service_tags` is the one reader of containers_list, and sharing it is what keeps
# "is this name a deploy tag?" answered identically here and in deploy.sh's own validation.
from deploy_tools.deploy_lib import tags as deploy_tags

# Bare, from scripts/deploy_tools, inserted above.
import narrow_setup
import reach

# Re-exported, not defined here: the path-to-role mappers live in `reach` and the shared-role
# expansion built on them in `land_shared`. Every reader keeps its old name, and
# `test_land_tags_shared_mapper_agreement.py` keeps pinning `land_tags.role_for`.
from deploy_tools.land_lib.land_shared import (  # noqa: F401  (`role_of` has an out-of-module reader)
    declared_tags,
    guard_map_rows,
    is_role_test_path,
    role_for,
    role_of,
    shared_caller_tags,
    shared_roles,
)

# The remediation for a rotated secret. Flat text rather than derived from the file list,
# because the consuming role is not knowable from here: a secret's value lives in no role's
# template, so the changed-path matching every other rule uses has nothing to match on.
#
# Named for the rotation it describes, not for the file that triggers it. CodeQL classifies a
# constant whose name reads as `SECRET` as sensitive data, so `print(plane_note(...))` was
# reported as py/clear-text-logging-sensitive-data every time a refactor moved that line. This
# constant is operator prose and holds no credential.
_ROTATION_NOTE = (
    "`ansible/vars/secrets.yml` changed, and a secret's VALUE lives in no role's template — so "
    "a rotation **maps to no deploy tag by construction**, and a consumer keeps rendering the "
    "OLD value until its own role renders again. Resolve them: `uv run python "
    "scripts/secrets_mgmt/secret_rotation.py consumers <secret>` greps the tree for who now "
    "holds a stale copy and prints the exact repair command per plane. Where it names a "
    "`CROSS_HOST_PUSH_TOKENS` member — the set `consumer_tags()` in that same file returns "
    "EMPTY for — the two halves sit on different hosts or planes, so no single redeploy covers "
    "both and each carries a written reason for what to run instead. The GitOps tick applies "
    "a setup role that reads a changed key and that its own host runs (#5436)."
)

# Which k8s consumers the tick applies itself, appended to `_ROTATION_NOTE`. The tick routes a
# secrets-only range to the k8s roles reading a moved key (#5577), and skips that route when the
# range also changes a k8s role (`deploy_broad_k8s.route_secret_k8s_readers`). A range with a
# deploy-plane path skips it too, because the deploy plane's narrowing already maps each moved
# key to its readers (#5409), and this landing waits on that apply's receipt. The lander sees
# only this PR's paths, not the tick's range, so the first note names the case where the tick
# routes nothing after all, and where to read what it applied. A secrets-only landing does not
# wait on the tick (`self_applied` is False), so that note says to read the line only once the
# tick has run.
_TICK_K8S_READERS_NOTE = (
    "It also applies each k8s role that reads a changed key (#5577). It applies none when its "
    "range also carries another session's k8s role change, and it records a reader the lookup "
    "refuses in `k8s_unapplied`. A Flux-owned reader is Flux's to apply: `deploy.sh` refuses "
    "its tag with exit 5. This landing does not wait for the tick, so wait until the tick "
    "crossing this merge has run (the `gitops-tick` skill runs one now). Its `secret-readers:` "
    "line in `journalctl -u gitops-deploy` then names what it applied, and only the consumers "
    "that line leaves out need a hand redeploy."
)
_DEPLOY_PLANE_K8S_READERS_NOTE = (
    "This PR also changes the deploy plane, so the tick's `deploy.yml` run covers each k8s role "
    "that reads a changed key (#5409), and this landing waits on that apply."
)
_NO_TICK_K8S_READERS_NOTE = (
    "This PR also changes a k8s role, so the tick applies no k8s role that reads a changed "
    "key (#5577): redeploy each k8s consumer by hand."
)

# The two files whose change moves the Secret `homelab-cluster-vars`, which every
# substituting Kustomization reads through `dependsOn: cluster-vars` (D3.4, d3-secrets.md,
# *Rotating a value read by both planes*). A PR changing one touches no role, so no tag
# derives from it, and `promotion.wait_on_promotion` waits on `cluster-vars` instead. The
# rule names no changed key, which keeps it within the `DECIDED: fire on ANY change to
# secrets.yml` marker below. It is interim: it retires in the PR where `land.sh` reads its
# changed set from `digests.json` (D6).
CLUSTER_VARS_PATHS = frozenset(
    {"ansible/vars/cluster-vars.sops.env", "ansible/vars/cluster-vars.source"}
)


def changes_cluster_vars(paths) -> bool:
    """Whether the PR changes a cluster file, so its landing waits on `cluster-vars`."""
    return any(p in CLUSTER_VARS_PATHS for p in paths)


def landing_hosts_at(
    tags, ref: str, cwd: Path, k8s_only=()
) -> dict[str, list[str]] | None:
    """`deploy_tags.landing_hosts`, with containers_list read AT `ref` rather than a checkout.

    A landing that deploys its PR's merge commit (`deploy.sh --at`) must route each tag from
    that commit's inventory: a PR adding a Pi role and its containers_list entry together
    declares the role on daniel-pi in NO checkout until the tick fast-forwards, so the primary
    read would route it to no host and the landing would run deploy.sh locally without
    `-e target=`. Same routing rule and the same staging exclusion as the tree read, through
    the one definition of each.

    None when the ref cannot be read, for `service_records_at_or_none`'s reason: an empty
    answer here would route every tag to no host, and the caller falls back to the tree.

    `k8s_only` is `hosts_for_tags`' per-tag platform restriction, and the landing passes its
    caller-expanded tags. `land_lib/deploy.deploy_by_host` is the one caller that
    fills it.
    """
    records = service_records_at_or_none(ref, cwd)
    if records is None:
        return None
    return landing_hosts_for_tags(tags, records, k8s_only)


def own_narrow_tags(files, pr_range: str, repo) -> dict[str, frozenset[str]]:
    """`narrow_setup.role_tags` over this PR's own range, per setup role it touches.

    A landing that never awaits the tick prints this directly: no receipt exists for its
    range yet, and the narrowed clear drops only these tags, so a wider row recorded later
    keeps the rest. A role whose derivation refuses or raises is left out and keeps its role
    tag.

    A role the range reaches no host through maps to an EMPTY set (#4408), which
    `plane_note` reads as "leave the role out", the way the tick drops it from its run. No
    other caller of `plane_note` passes an empty set: `classify.narrow_plane` filters the
    receipt's empty rows out before rendering, because a receipt stores a refusal that way.
    """
    if ".." not in pr_range:
        return {}
    old, new = pr_range.split("..", 1)
    out: dict[str, frozenset[str]] = {}
    for role in reach.reach(list(files)).changes.setup_roles:
        tag, playbook = setup_role_tag(role), setup_role_playbook(role)
        if playbook is None:
            continue
        # DECIDED: `except Exception`, because any failure here must print the role tag. The
        # derivation shells out to git and decodes the output. An escape would kill a landing
        # that has already merged, over a note the whole-role tag answers correctly.
        try:
            out[tag] = narrow_setup.role_tags(role, tag, old, new, str(repo), playbook)
        except narrow_setup.NothingToApply as exc:
            # Before the generic arm, which would read it as a refusal: it subclasses
            # `CannotNarrow` so that a caller knowing only the refusal keeps the role tag.
            print(f"narrow-setup: {role} needs no tag ({exc})", file=sys.stderr)
            out[tag] = frozenset()
        except Exception as exc:
            print(f"narrow-setup: {role} keeps its role tag ({exc})", file=sys.stderr)
    return out


def _needs_nothing(role: str, narrow_tags: dict[str, frozenset[str]] | None) -> bool:
    """Whether `own_narrow_tags` answered that this PR's range reaches no host through `role`.

    The key must be PRESENT with an empty set. An absent key is a refusal, and a refusal
    keeps the whole-role tag.
    """
    tag = setup_role_tag(role)
    return narrow_tags is not None and tag in narrow_tags and not narrow_tags[tag]


def plane_note(
    files,
    declared: set[str] | None = None,
    quiet=(),
    narrow_tags: dict[str, frozenset[str]] | None = None,
    roles_root=None,
) -> str:
    """What this PR still needs a HUMAN to apply, or "" if nothing.

    A deploy tag covers roles/k8s and roles/containers. It does not cover the setup plane,
    which `deploy.yml` cannot apply at all -- so a PR touching only roles/setup derives zero
    tags. That must not read as `nothing-to-deploy`: a setup-role change needs its own
    `initial_setup.yml --tags <role>` run.

    Returned for the tag-carrying case too. A PR can touch a k8s role AND the setup plane,
    where the deploy genuinely succeeds and half the change is still unapplied -- the harder
    version of the same silence, because the verdict reads `settled`.

    A shared k8s role is the same shape, one plane over: `--tags manifests` matches nothing.
    The landing deploys its callers instead (`shared_caller_tags`), so only a shared
    role that no declared role runs is still named here. `shared_role_reach` drops the paths
    of one whose change reaches no rendered manifest before either reads them.

    A rotated secret is the third shape, and the one with no path to match at all. A secret's
    value lives in no role's template, so `ansible/vars/secrets.yml` derives zero tags however
    many roles consume it: every consumer keeps rendering the old value, and the landing
    would report `nothing-to-deploy`.

    `quiet` is the broad-plane paths whose diff carries no content change, from
    `deploy_tags.comment_only_paths`. Here they are dropped from the BROAD half only: a playbook
    named for three edited comments has nothing to apply. The secrets half reads the unfiltered
    list -- `comment_only_broad_changes` cannot return `ansible/vars/secrets.yml`, and
    keeping the reads separate means a later widening there cannot silently mute a rotation.

    `narrow_tags` is role tag -> the tags that role's change needs. A role mapped to an
    empty set needs none over this range (`own_narrow_tags`), so the note leaves it out,
    clear command included: `deploy_defer.record` writes no `manual_plane` line for it.

    `roles_root` is the role tree `land_shared.shared_roles` checks for a deleted role, for a
    test that needs one present; `None` reads the repo's own.
    """
    files = list(files)
    quiet = set(quiet)
    declared = declared_tags() if declared is None else declared
    notes = []
    # DECIDED: fan a shared role out to its callers, reversing the "report, do not fan out"
    # this held until #2704. The operator asked for every piece of the deploy to be
    # selectable, and a report left PR #2701 for the next full deploy. A change to
    # `manifests` or `volume-snapshot` therefore deploys ~58 services — about a full deploy,
    # unmeasured as a tag list. A change reaching no rendered manifest never gets here:
    # `shared_role_reach` drops its paths first. The deployer's `k8s_remediation` keeps the
    # report, because an unattended tick parking a range is a different cost.
    expanded = shared_caller_tags(files, declared, roles_root)
    shared = [r for r in shared_roles(files, declared, roles_root) if not expanded[r]]
    if shared:
        # Passing ONLY the shared half. k8s_remediation appends a scoped `--tags` line for
        # any deployable role it is given, and land.sh has already deployed those itself.
        notes.append(k8s_remediation(set(shared), declared))
    cs = reach.reach(files).changes
    # Only the broad changes the deployer will NOT apply itself are owed to a human. The tick
    # fast-forwards and applies a deploy-plane change as a full deploy.yml and a setup-plane
    # change as `initial_setup.yml --tags <role>`, including the deployer's own role.
    # What is left for a hand: the bring-up playbooks
    # (`_BROAD_MANUAL_PREFIXES`, which park the tick outright), and a setup role that
    # initial_setup.yml does not include (k3s lives in k3s-bringup.yml, common in no playbook)
    # or gates off the tick's host (optimize_pi, #3933), for which `setup_tags_for` derives
    # nothing and the tick defers. Reporting the self-applied
    # roles here would make land.sh exit 1 with `needs-manual-apply` while the next tick is
    # applying exactly those roles. land.sh reads the deployer's own state for
    # that case instead.
    loud = reach.reach(files, quiet)
    unroutable = {
        r
        for r in loud.changes.setup_roles
        if not tick_applies_setup_role(r) and not _needs_nothing(r, narrow_tags)
    }
    if loud.manual or unroutable:
        notes.append(
            broad_remediation(
                False,
                True,
                unroutable,
                narrow_tags=narrow_tags if narrow_tags is not None else {},
                playbooks=loud.changes.manual_playbooks,
            )
        )
    if unroutable and not loud.manual:
        # The tick MERGED this PR and recorded the role in `manual_plane`, so applying it by
        # hand is only half the job: a role left in the marker pages GitOps Deploy — Status
        # six hours later over work that is already live.
        #
        # `not manual` because `deploy_defer.parks_the_tick` gives `cs.broad_manual` priority:
        # a range carrying BOTH a bring-up playbook and an unapplyable role parks outright and
        # writes no marker, so printing the clear command there sends an operator after a file
        # that does not exist.
        # `--applied` where the apply above was narrowed; see `manual_plane_clear_for`.
        cmd = manual_plane_clear_for(unroutable, narrow_tags or {})
        notes.append(f"Then clear the deployer's marker: `{cmd}`.")
    if cs.secrets:
        # DECIDED: fire on ANY change to secrets.yml, and never try to name which keys moved.
        # Naming them means decrypting both revisions, and no plaintext may reach a terminal, a
        # transcript or a log. `.gitattributes` sets `diff=sops`, so even `git diff
        # ansible/vars/secrets.yml` renders the values, which is why a hook denies that form.
        # Over-firing on a key that needed no redeploy costs one exit code; under-firing leaves
        # a rotated credential stale in a consumer this file list cannot show.
        #
        # DECIDED: fire on the tag-carrying path too, which is the opposite of what
        # deploy_logic.alert_secrets_deferred does for the deployer. Not a contradiction, a
        # different reader: that alert is unattended, so a false fire on the /add-secret happy
        # path is noise nobody can act on. Here an operator is reading land.sh's output, and a
        # PR shipping secrets.yml WITH one consuming template still cannot show the OTHER
        # consumers -- PR #695's token had two, in two planes, and the landing got neither.
        #
        # `cs.secrets` rather than a path literal, so this and the deployer answer "did a secret
        # change?" identically. It is exactly `ansible/vars/secrets.yml`: the registry
        # `ansible/secret_rotation.yml` carries names and dates but no values, and
        # `ansible/vars/secrets-staging.yml` belongs to daniel-stage, which land.sh never
        # deploys. Both are correctly outside it.
        #
        # The k8s half mirrors the tick's own routing, from this PR's paths alone. A deploy-plane
        # path sends the readers through that plane's narrowing, and a k8s role change routes no
        # reader, so the operator still owes every k8s consumer. A
        # Flux-owned role over-asks here, because the tick moves it to `cs.flux` before that
        # skip and so does route the readers. Asking for a redeploy the tick also ran costs a
        # duplicate apply; claiming one it skipped leaves a credential stale.
        notes.append(_ROTATION_NOTE)
        if cs.broad_deploy:
            notes.append(_DEPLOY_PLANE_K8S_READERS_NOTE)
        elif cs.k8s or cs.k8s_deploy or cs.k8s_consumers:
            notes.append(_NO_TICK_K8S_READERS_NOTE)
        else:
            notes.append(_TICK_K8S_READERS_NOTE)
    return " ".join(notes)


def self_applied_playbooks(files, quiet=()) -> frozenset[str]:
    """The playbooks the TICK runs for this PR's broad half; empty when it runs none.

    `self_applied` is whether this is non-empty. `Landing.tick_applied` asks the tick's
    receipt for each of them, because a receipt naming only the setup plane says nothing
    about a deploy plane the tick deferred (#4333).
    """
    cs = reach.reach(files, quiet).changes
    out = {setup_role_playbook(r) for r in cs.setup_roles if tick_applies_setup_role(r)}
    if cs.broad_deploy:
        out.add("ansible/deploy.yml")
    return frozenset(p for p in out if p)


def self_applied(files, quiet=()) -> bool:
    """Whether the PR carries a broad change that the TICK applies, not deploy.sh.

    When true, the landing is not done until the deployer's own state says it converged.
    True for a deploy-plane change (a full deploy.yml) and for a setup role
    initial_setup.yml includes (`--tags <role>`). False for docs, for an ordinary service
    (deploy.sh's job), and for what `plane_note` already hands to a human. land.sh uses it
    to decide whether `behind_since` after the tick means "this PR is not applied yet" or
    is merely somebody else's pending merge.

    `quiet` is dropped for the same reason `plane_note` drops it: a comment-only setup-role
    edit is nothing for the tick to apply, so waiting on the deployer's state to prove it
    did is waiting on a convergence that means something else.
    """
    return bool(self_applied_playbooks(files, quiet))


def self_applied_command(files, quiet=()) -> str:
    """What applies BY HAND the half of this PR the TICK normally applies itself, or "".

    Over exactly the paths `self_applied` answers True for, so the two cannot name different
    work. `broad_remediation` is the deployer's own text, ff-merge first — Ansible renders from
    the working tree, so a playbook run before the merge applies the PRE-merge files and reports
    `changed=0`.

    THE TICK CONVERGING IS NOT PROOF IT APPLIED ANYTHING. Any session's `git merge --ff-only`
    also makes local == origin, and from then on `next_action()` returns `noop` for every later
    tick — so the plane is stranded permanently and the one mechanism that would apply it never
    sees the range again. This is the line `land.sh` prints when the deployer
    recorded no apply covering the PR.
    """
    cs = reach.reach(files, quiet).changes
    routable = {r for r in cs.setup_roles if tick_applies_setup_role(r)}
    if not (cs.broad_deploy or routable):
        return ""
    return broad_remediation(cs.broad_deploy, bool(routable), routable)


class DeriveSource(StrEnum):
    """Where a tag list came from, and therefore whether it can be trusted whole."""

    PR = "pr"
    FALLBACK = "fallback"


class Derivation(NamedTuple):
    """What `derive` answers: the tags, and whether the file list could be trusted."""

    tags: list[str]
    source: DeriveSource


def derive(
    files, changed_files: int, declared: set[str] | None = None, quiet=()
) -> Derivation:
    """(sorted tags, 'pr'|'fallback').

    'fallback' means the file list could not be trusted and the caller must widen to a SHA
    range. The tag list returned alongside it is empty on purpose: a partial list is worse
    than none, because it looks like an answer.

    A changed role with no `containers_list` entry yields no tag. It is not dropped silently
    -- `plane_note` names it and what applies it -- because dropping it from BOTH would let the
    setup plane read as `nothing-to-deploy`.

    `quiet` is `quiet_paths`'s set. Of what it holds, only a comment-only k8s `defaults/` file
    maps to a tag, and the tick treats that edit as no change (#4460), so it deploys nothing.
    """
    files = list(files)
    if len(files) != changed_files:
        return Derivation([], DeriveSource.FALLBACK)
    declared = declared_tags() if declared is None else declared
    # A build role whose workload lives in a different role must not deploy alone: the build
    # would push a new image that nothing rolls onto, and report green doing it.
    return Derivation(
        sorted(reach.reach(files, quiet).tags(declared, REPO)), DeriveSource.PR
    )


def doc_paths(paths) -> set[str]:
    """The paths in `paths` that are documentation, which no playbook applies.

    A `.md` only. Not `docs/`, which matches no plane prefix anyway, and not a `.txt` or a
    `README` without a suffix -- the narrow predicate is the one that cannot swallow a file
    something renders. It is the same suffix test the deployer's own mapper makes.
    """
    return {p for p in paths if p.endswith(".md")}


def quiet_paths(paths: list[str], range_: str) -> set[str]:
    """The broad paths in `paths` a landing owes nothing for: docs, and comment-only edits.

    Documentation is quiet on its own terms, before any range is read: a `.md` under
    `roles/setup/k3s/` still takes the broad arm by prefix, so a docs-only PR there would end
    `needs-manual-apply` naming `k3s-bringup.yml` with nothing to apply. The comments-only
    test below cannot reach that case -- it reads
    YAML content lines, and prose is not comments.

    Empty of comment-only paths when no range was given, or when the range is malformed, or
    when git cannot read a side of it -- every one of those keeps the path broad, which is the
    direction a wrong answer here must fall. The docs half survives all three,
    because it asks nothing of the range.

    A RANGE NARROWER THAN THE FILE LIST is the same failure wearing a valid range, and it
    fails the unsafe way: a broad path substantively changed OUTSIDE the range reads as
    identical on both sides, so it comes back quiet and its manual apply is skipped. The PR
    file list is the whole PR; a range covering only part of it cannot answer for the rest.
    So every path asked about must appear in the range's own diff, or nothing is quiet --
    the same shape as `derive`'s count assertion, and for the same reason: wider than the
    truth is recoverable, narrower is not.
    """
    docs = doc_paths(paths)
    old, sep, new = range_.partition("..")
    if not (sep and old and new):
        return docs
    try:
        covered = set(deploy_tags.range_paths(old, new))
        if not set(paths) <= covered:
            return docs
        return docs | deploy_tags.comment_only_paths(paths, old, new)
    except subprocess.CalledProcessError, OSError:
        return docs


# The CLI moved to `land_tags_cli.py`, above `shared_role_reach` (#4945). Run directly, this
# module refuses rather than printing nothing, because an empty answer reads as "no tags".
_MOVED = (
    "land_tags.py is a library. Its CLI is "
    "scripts/deploy_tools/land_lib/land_tags_cli.py, with the same arguments."
)

if __name__ == "__main__":
    answer_help(_MOVED)
    print(_MOVED, file=sys.stderr)
    sys.exit(USAGE_ERROR)
