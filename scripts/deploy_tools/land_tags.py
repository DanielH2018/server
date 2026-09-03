#!/usr/bin/env python3
"""Derive deploy tags from a merged PR's own file list.

WHY NOT A SHA RANGE. `deploy.sh --changed <ref>` diffs a commit range, and after a merge
that range covers every other session's merged work too. Deploying somebody else's
half-finished landing is not this session's to do. A PR's file list is exactly this
session's scope.

WHY A ROLE NAME IS NOT AUTOMATICALLY A TAG. Only a role with a `containers_list` entry has
one. Eight roles under ansible/roles/k8s/ have no entry because other roles include them by
literal name, and handing one to `--tags` makes deploy.sh refuse the WHOLE list (exit 2) --
so the valid services beside it are refused too. PR #617 landed 22 digest pins that way and
none of them deployed. Those roles come out of the tags and into `plane_note` instead.

WHY THE COUNT ASSERTION. `gh pr view --json files` paginates at 100. A 137-file PR returns
100 entries with no error and no marker, so the derived tag list is a silent subset of what
merged -- and every downstream check reads green over it. When the returned count disagrees
with the PR's own `changedFiles`, this reports `fallback` and the caller widens to
`deploy.sh --changed <since>`: wider than the truth is recoverable, narrower is not.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


# The build/roll couplings live in deploy_logic so this and `deploy_tags.py changed` widen
# identically -- two derivations that disagree is the defect this import exists to prevent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, ANSIBLE, GITOPS_DEPLOY_FILES, HOST_VARS

sys.path.insert(0, str(GITOPS_DEPLOY_FILES))

from deploy_logic import (
    _BROAD_MANUAL_PREFIXES,
    broad_remediation,
    expand_build_couplings,
    k8s_remediation,
    services_from_changed_paths,
    setup_role_playbook,
    setup_role_tag,
)

# Same directory, so a direct invocation already has it on sys.path. `service_tags` is the
# one reader of containers_list, and sharing it is what keeps "is this name a deploy tag?"
# answered identically here and in deploy.sh's own validation.
import deploy_tags

_K8S = re.compile(r"^ansible/roles/k8s/([^/]+)/")
_DOCKER = re.compile(r"^ansible/roles/containers/([^/]+)/")

# Directories under the role trees that are not services. `common` is the shared Docker
# deploy path and `archive` holds roles retired by the k3s migration; `--tags` for either
# matches no containers_list entry, and Ansible exits 0 on a tag that selects nothing --
# so a green run would prove only that nothing happened.
_NOT_SERVICES = frozenset({"common", "archive"})

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
    "a rotation **maps to no deploy tag by construction** and every consumer keeps rendering "
    "the OLD value until its own role is redeployed. Resolve them: `uv run python "
    "scripts/secrets_mgmt/secret_rotation.py consumers <secret>` greps the tree for who now "
    "holds a stale copy and prints the exact repair command per plane. Where it names a "
    "`CROSS_HOST_PUSH_TOKENS` member — the set `consumer_tags()` in that same file returns "
    "EMPTY for — the two halves sit on different hosts or planes, so no single redeploy covers "
    "both and each carries a written reason for what to run instead."
)


def declared_tags() -> set[str]:
    """Every name that selects a service, read from containers_list."""
    return deploy_tags.service_tags()


def role_for(path: str) -> str | None:
    """The role directory a changed path belongs to, or None.

    Not the same question as `tag_for`: a role directory under roles/k8s/ need not have a
    `containers_list` entry, and eight of them do not.
    """
    for pattern in (_K8S, _DOCKER):
        m = pattern.match(path)
        if m and m.group(1) not in _NOT_SERVICES:
            return m.group(1)
    return None


def tag_for(path: str, declared: set[str] | None = None) -> str | None:
    """The deploy tag a changed path maps to, or None."""
    role = role_for(path)
    if role is None:
        return None
    declared = declared_tags() if declared is None else declared
    return role if role in declared else None


def shared_roles(files, declared: set[str] | None = None) -> list[str]:
    """The changed role directories that have no `containers_list` entry.

    These are the shared k3s plane — `manifests` is the apply-and-roll path every workload
    includes, `volume-claim` and `volume-revert` are storage paths several include. Naming one
    in `--tags` makes deploy.sh refuse the ENTIRE list (exit 2), so they must be split off the
    tags and reported as work a human still owes. PR #617 is the measured case.
    """
    declared = declared_tags() if declared is None else declared
    roles = {r for p in files if (r := role_for(p))}
    return sorted(roles - declared)


def plane_note(files, declared: set[str] | None = None, quiet=()) -> str:
    """What this PR still needs a HUMAN to apply, or "" if nothing.

    A deploy tag covers roles/k8s and roles/containers. It does not cover the setup plane,
    which `deploy.yml` cannot apply at all -- so a PR touching only roles/setup derives zero
    tags and land.sh used to call that `nothing-to-deploy`. True about service tags, silent
    about the operator: PR #587 needed `initial_setup.yml --tags gitops_deploy` and was
    reported as needing nothing (2026-08-29).

    Returned for the tag-carrying case too. A PR can touch a k8s role AND the setup plane,
    where the deploy genuinely succeeds and half the change is still unapplied -- the harder
    version of the same silence, because the verdict reads `settled`.

    A shared k8s role is the same shape, one plane over: `--tags manifests` matches nothing,
    so no derived tag can ever apply it and only a full deploy will. Reusing this note rather
    than minting a verdict keeps one meaning for "landed, not live".

    A rotated secret is the third shape, and the one with no path to match at all. A secret's
    value lives in no role's template, so `ansible/vars/secrets.yml` derives zero tags however
    many roles consume it: PR #695 rotated `ruleset_drift_push_token`, whose two consumers --
    the uptime-kuma tile and the gitops_deploy pusher cron -- both kept rendering the old
    value, and this reported `nothing-to-deploy` (2026-09-01).

    `quiet` is the broad-plane paths whose diff carries no content change, from
    `deploy_tags.comment_only_paths`. They are dropped from the BROAD half only: a playbook
    named for three edited comments has nothing to apply, and PR #843 ended
    `needs-manual-apply` for exactly that (issue #848). The secrets half reads the unfiltered
    list -- `comment_only_broad_changes` cannot return `ansible/vars/secrets.yml`, and
    keeping the reads separate means a later widening there cannot silently mute a rotation.
    """
    files = list(files)
    quiet = set(quiet)
    declared = declared_tags() if declared is None else declared
    notes = []
    shared = shared_roles(files, declared)
    if shared:
        # DECIDED: report the shared plane, do not fan it out to its dependents. 53 roles
        # include k8s/manifests, so a fan-out is a full deploy wearing a tag list — 20
        # minutes of run time, and every rollout gate and stabilisation window with it — for
        # what is usually a two-line change. Reporting it keeps the operator's choice of
        # when. deploy_logic.k8s_remediation reached the same conclusion for the deployer's
        # alert path and carries the longer argument, including why routing it to `cs.broad`
        # is worse than either.
        # Passing ONLY the shared half. k8s_remediation appends a scoped `--tags` line for
        # any deployable role it is given, and land.sh has already deployed those itself.
        notes.append(k8s_remediation(set(shared), declared))
    cs = services_from_changed_paths(files)
    # Only the broad changes the deployer will NOT apply itself are owed to a human. Since
    # 2026-08-29 the tick fast-forwards and applies a deploy-plane change as a full deploy.yml
    # and a setup-plane change as `initial_setup.yml --tags <role>`; since #719 that includes
    # the deployer's own role. What is left for a hand: the bring-up playbooks
    # (`_BROAD_MANUAL_PREFIXES`, which park the tick outright), and a setup role that
    # initial_setup.yml does not include (k3s lives in k3s-bringup.yml, common in no playbook),
    # for which `setup_tags_for` derives nothing and the tick defers. Reporting the self-applied
    # roles here made land.sh exit 1 with `needs-manual-apply` for #723 while the next tick was
    # applying exactly those roles (2026-09-01). land.sh reads the deployer's own state for
    # that case instead.
    loud = [p for p in files if p not in quiet]
    manual = [p for p in loud if any(p.startswith(x) for x in _BROAD_MANUAL_PREFIXES)]
    unroutable = {
        r
        for r in services_from_changed_paths(loud).setup_roles
        if setup_role_playbook(r) != "ansible/initial_setup.yml"
    }
    if manual or unroutable:
        notes.append(broad_remediation(False, True, unroutable))
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
        notes.append(_ROTATION_NOTE)
    return " ".join(notes)


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
    quiet = set(quiet)
    cs = services_from_changed_paths([p for p in files if p not in quiet])
    if cs.broad_deploy:
        return True
    return any(
        setup_role_playbook(r) == "ansible/initial_setup.yml" for r in cs.setup_roles
    )


# The hosts land.sh's setup-role remediation ever names. daniel-stage is excluded on
# purpose -- it is not land.sh's business (HOSTS_LAND_SH_NEVER_DEPLOYS in deploy_tags.py is
# the same exclusion for a deploy tag), and initial_setup.yml is never run against it from
# here.
_HOSTS = ("daniel-box", "daniel-server", "daniel-pi")

# `ansible_connection=local` in hosts.ini -- selecting one of these with `-e target=` from
# elsewhere only picks its VARIABLES; the play still runs on whichever host you typed the
# command on (hosts.ini's own comment, ENFORCED by
# ansible/tests/deploy/test_local_connection_target.py). So a remaining host in this set
# must be reached by sshing to it first. daniel-pi is the one host actually driven remotely
# with `-e target=daniel-pi`, from wherever the play runs.
_LOCAL_CONNECTION_HOSTS = frozenset({"daniel-box", "daniel-server"})

_INITIAL_SETUP_YML = ANSIBLE / "initial_setup.yml"


def _initial_setup_roles(playbook: Path = _INITIAL_SETUP_YML) -> dict[str, object]:
    """{role name: its `when:` value (a string, a list, a bool, or None)}.

    Read from `playbook`'s own `roles:` list -- the same source Ansible itself resolves
    against, so this can never disagree with what a real run does. `playbook` defaults to
    this repo's `initial_setup.yml`; a test passes a synthetic one so the derivation it pins
    cannot drift when this repo's own gates change (mirrors `deploy_tags.py`'s
    `host_vars: Path = HOST_VARS` pattern).
    """
    play = yaml_fast.safe_load(playbook.read_text())[0]
    roles: dict[str, object] = {}
    for entry in play["roles"]:
        name = entry if isinstance(entry, str) else entry["role"]
        when = None if isinstance(entry, str) else entry.get("when")
        roles[name] = when
    return roles


def _host_vars(
    host: str, all_vars: Path = ALL_VARS, host_vars_dir: Path = HOST_VARS
) -> dict:
    """`all_vars` overridden by `host_vars_dir`/<host>.yml.

    The same precedence Ansible resolves a `when:` variable through (a host_vars key always
    wins over the group default). Defaults to this repo's group_vars/all.yml and host_vars/.
    """
    merged = dict(yaml_fast.safe_load(all_vars.read_text()) or {})
    hv = host_vars_dir / f"{host}.yml"
    if hv.exists():
        merged.update(yaml_fast.safe_load(hv.read_text()) or {})
    return merged


def _eval_when(
    expr: object, host: str, all_vars: Path = ALL_VARS, host_vars_dir: Path = HOST_VARS
) -> bool:
    """Best-effort read of a `when:` value for one host.

    Every gate `initial_setup.yml` uses today is a bare var, an `or`/`and` of them, an
    `inventory_hostname == <var-or-literal>` comparison, or one of those with a trailing
    `| bool` filter -- all valid Python once `| bool` is stripped, so `eval` against the
    host's merged vars reads them exactly as Ansible would. A YAML list is Ansible's
    implicit AND (`when: [a, b]` means `a and b`), so it is joined before evaluating rather
    than rejected.

    Returns True -- host REACHED -- whenever evaluation cannot be trusted: a non-string,
    non-list value (a YAML `when: true`), an unresolved name, or a Jinja construct `eval`
    cannot parse. Wider than the truth is recoverable (an extra command an operator can
    no-op past); narrower silently hides a real gap, which is the failure this function
    exists to close. Same asymmetry `_quiet_paths` already applies to a broad path it cannot
    read.
    """
    if isinstance(expr, list):
        expr = " and ".join(f"({e})" for e in expr)
    if not isinstance(expr, str):
        return True
    ns = dict(_host_vars(host, all_vars, host_vars_dir))
    ns["inventory_hostname"] = host
    py_expr = expr.replace("| bool", "").replace("|bool", "")
    try:
        return bool(eval(py_expr, {"__builtins__": {}}, ns))
    except Exception:
        return True


def setup_role_hosts(
    role: str,
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
) -> frozenset[str]:
    """Which of `_HOSTS` `initial_setup.yml` applies `role` to.

    THE HOLE THIS CLOSES. `self_applied()` says a setup role is the tick's to apply, but the
    tick only ever runs on ONE host -- the one `gitops_deploy` is armed on (`has_gitops`,
    daniel-box only: `roles: [{role: gitops_deploy, when: has_gitops}, ...]` in
    initial_setup.yml, and `has_gitops` is true only in daniel-box's host_vars).
    `initial_setup.yml`'s own `hosts:` is `{{ target | default(lookup('pipe','hostname')) }}`
    -- one host per run -- so a role with NO `when:` gate (`initial_setup` itself among them)
    reaches every host the playbook is EVER run on, and the tick converging on daniel-box says
    nothing about the other two. Issue #1009: PR #1002 changed
    `roles/setup/initial_setup/files/kuma-push-lib.sh`, the tick converged, and land.sh read
    `settled` while daniel-server and daniel-pi kept running the old library.

    Returns an empty set for a role `initial_setup.yml` does not reach at all (its playbook
    is not `ansible/initial_setup.yml`, or it is not in that playbook's `roles:` list) --
    that is `plane_note`'s `unroutable` territory, not this function's to guess at.
    """
    if setup_role_playbook(role) != "ansible/initial_setup.yml":
        return frozenset()
    roles = _initial_setup_roles(playbook)
    if role not in roles:
        return frozenset()
    when = roles[role]
    if when is None:
        return frozenset(_HOSTS)
    return frozenset(h for h in _HOSTS if _eval_when(when, h, all_vars, host_vars_dir))


def _setup_apply_command(role: str, host: str) -> str:
    """The exact command that applies `role` on `host` via initial_setup.yml.

    Mirrors `deploy_remediation._setup_commands`'s hand-written pair for the `common` role
    (ssh-then-run for a local-connection host, `-e target=` for daniel-pi) -- generalised to
    any role/host pair rather than that one role's two consumers.

    A `_LOCAL_CONNECTION_HOSTS` remote host (daniel-server, when it is not `local_host`)
    renders from ITS OWN checkout: `ansible_connection=local` means the play there runs as
    its own controller against its own `/home/ubuntu/server`, and nothing keeps that current
    -- the crons that `git pull` (secret-rotate.sh.j2, docs-refresh.sh.j2) are both `when:
    has_gitops`, daniel-box only. Skipping the pull renders the PRE-merge tree and reports
    `changed=0`, the exact trap `broad_remediation`'s docstring records an operator hitting
    on 2026-09-01 -- so the pull is folded into the same command rather than left as a
    separate step a copy-paste can drop. daniel-pi has no such hazard: `-e target=daniel-pi`
    renders on THIS host's already-current checkout and only executes remotely over SSH.
    """
    tag = setup_role_tag(role)
    if host in _LOCAL_CONNECTION_HOSTS:
        return (
            f'`ssh {host} "cd /home/ubuntu/server && git pull --ff-only && '
            f'ansible-playbook ansible/initial_setup.yml --tags {tag}"`'
        )
    return f"`ansible-playbook ansible/initial_setup.yml --tags {tag} -e target={host}`"


def remaining_setup_hosts_note(
    files,
    local_host: str,
    quiet=(),
    playbook: Path = _INITIAL_SETUP_YML,
    all_vars: Path = ALL_VARS,
    host_vars_dir: Path = HOST_VARS,
) -> str:
    """What a self-applied setup-role change still needs beyond `local_host`, or "" if nothing does.

    `local_host` is the host the tick just ran on. Additive to `plane_note`'s `unroutable`
    case, not a duplicate of it: that flags a role no playbook ever reaches; this flags a
    role `initial_setup.yml` DOES reach, on hosts the tick's single run never touches.

    Empty for the #723 shape -- `gitops_deploy` is `when: has_gitops`, true only on
    daniel-box, so a PR touching only a role whose sole reached host is `local_host` stays
    unowed to a hand, exactly as `plane_note` already keeps it.
    """
    quiet = set(quiet)
    cs = services_from_changed_paths([p for p in files if p not in quiet])
    remaining: dict[str, frozenset[str]] = {}
    for role in cs.setup_roles:
        hosts = setup_role_hosts(role, playbook, all_vars, host_vars_dir) - {local_host}
        if hosts:
            remaining[role] = hosts
    if not remaining:
        return ""
    return "; ".join(
        f"`{role}` also reaches {host} (not applied by this tick): "
        f"{_setup_apply_command(role, host)}"
        for role in sorted(remaining)
        for host in sorted(remaining[role])
    )


def derive(
    files, changed_files: int, declared: set[str] | None = None
) -> tuple[list[str], str]:
    """(sorted tags, 'pr'|'fallback').

    'fallback' means the file list could not be trusted and the caller must widen to a SHA
    range. The tag list returned alongside it is empty on purpose: a partial list is worse
    than none, because it looks like an answer.

    A changed role with no `containers_list` entry yields no tag. It is not dropped silently
    -- `plane_note` names it and what applies it -- because dropping it from BOTH is how the
    setup plane used to read as `nothing-to-deploy`.
    """
    files = list(files)
    if len(files) != changed_files:
        return [], "fallback"
    declared = declared_tags() if declared is None else declared
    # A build role whose workload lives in a different role must not deploy alone: the build
    # would push a new image that nothing rolls onto, and report green doing it.
    tags = expand_build_couplings({t for p in files if (t := tag_for(p, declared))})
    return sorted(tags), "pr"


def quiet_paths(paths: list[str], range_: str) -> set[str]:
    """The broad paths in `paths` whose change over `<old>..<new>` is comments only.

    Empty when no range was given, or when the range is malformed, or when git cannot read
    a side of it -- every one of those keeps the path broad, which is the direction a wrong
    answer here must fall (issue #848).

    A RANGE NARROWER THAN THE FILE LIST is the same failure wearing a valid range, and it
    fails the unsafe way: a broad path substantively changed OUTSIDE the range reads as
    identical on both sides, so it comes back quiet and its manual apply is skipped. The PR
    file list is the whole PR; a range covering only part of it cannot answer for the rest.
    So every path asked about must appear in the range's own diff, or nothing is quiet --
    the same shape as `derive`'s count assertion, and for the same reason: wider than the
    truth is recoverable, narrower is not.
    """
    old, sep, new = range_.partition("..")
    if not (sep and old and new):
        return set()
    try:
        covered = set(deploy_tags.range_paths(old, new))
        if not set(paths) <= covered:
            return set()
        return deploy_tags.comment_only_paths(paths, old, new)
    except subprocess.CalledProcessError, OSError:
        return set()


def main(argv: list[str] | None = None) -> int:
    """Print one fact about a PR's file list -- tags, plane note, self-applied flag, or remaining-setup-hosts note.

    Which one prints depends on `--plane`/`--self-applied`/`--remaining-setup-hosts`; with
    none of them, prints the derived `--tags` value. Always exits 0.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", required=True, help="`gh pr view --json files,changedFiles` output"
    )
    parser.add_argument(
        "--plane",
        action="store_true",
        help="print what still needs a manual apply (empty if nothing), not the tags",
    )
    parser.add_argument(
        "--self-applied",
        action="store_true",
        help="print `yes` if the tick applies part of this PR itself (empty otherwise)",
    )
    parser.add_argument(
        "--remaining-setup-hosts",
        metavar="HOST",
        default=None,
        help=(
            "print what a self-applied setup role in this PR still needs beyond HOST -- "
            "the host the tick just ran on (empty if nothing)"
        ),
    )
    parser.add_argument(
        "--range",
        dest="range_",
        default="",
        help="`<old>..<new>` — read the diff to drop broad paths whose change is comments only",
    )
    ns = parser.parse_args(argv)
    payload = json.loads(ns.json)
    paths = [f["path"] for f in payload.get("files", [])]
    quiet = quiet_paths(paths, ns.range_)
    if ns.plane:
        print(plane_note(paths, quiet=quiet))
        return 0
    if ns.self_applied:
        print("yes" if self_applied(paths, quiet=quiet) else "")
        return 0
    if ns.remaining_setup_hosts is not None:
        print(remaining_setup_hosts_note(paths, ns.remaining_setup_hosts, quiet=quiet))
        return 0
    tags, source = derive(
        [f["path"] for f in payload.get("files", [])],
        # -1 rather than 0: `gh` omitting the field must not be read as agreement with an
        # empty file list, which would silently license a zero-tag deploy.
        payload.get("changedFiles", -1),
    )
    print(f"{source} {','.join(tags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
