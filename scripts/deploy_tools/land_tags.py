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

Run: uv run pytest scripts/deploy_tools/test_land_tags.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The build/roll couplings live in deploy_logic so this and `deploy_tags.py changed` widen
# identically -- two derivations that disagree is the defect this import exists to prevent.
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "ansible"
        / "roles"
        / "setup"
        / "gitops_deploy"
        / "files"
    ),
)

from deploy_logic import (  # noqa: E402 — needs the path insert above
    broad_remediation,
    expand_build_couplings,
    k8s_remediation,
    services_from_changed_paths,
)

# Same directory, so a direct invocation already has it on sys.path. `service_tags` is the
# one reader of containers_list, and sharing it is what keeps "is this name a deploy tag?"
# answered identically here and in deploy.sh's own validation.
import deploy_tags  # noqa: E402

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
_SECRETS_NOTE = (
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
    includes, `seed-volume` and `volume-revert` are storage paths several include. Naming one
    in `--tags` makes deploy.sh refuse the ENTIRE list (exit 2), so they must be split off the
    tags and reported as work a human still owes. PR #617 is the measured case.
    """
    declared = declared_tags() if declared is None else declared
    roles = {r for p in files if (r := role_for(p))}
    return sorted(roles - declared)


def plane_note(files, declared: set[str] | None = None) -> str:
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
    """
    files = list(files)
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
    if cs.broad_setup or cs.broad_deploy:
        notes.append(broad_remediation(cs.broad_deploy, cs.broad_setup, cs.setup_roles))
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
        notes.append(_SECRETS_NOTE)
    return " ".join(notes)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json", required=True, help="`gh pr view --json files,changedFiles` output"
    )
    parser.add_argument(
        "--plane",
        action="store_true",
        help="print what still needs a manual apply (empty if nothing), not the tags",
    )
    ns = parser.parse_args(argv)
    payload = json.loads(ns.json)
    if ns.plane:
        print(plane_note([f["path"] for f in payload.get("files", [])]))
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
