#!/usr/bin/env python3
"""Which narrow `--tags` value a setup-role change needs, or a refusal to guess.

THE PROBLEM. The deployer's deferral remediation names the whole setup ROLE tag. For
`roles/setup/k3s/` that is `ansible-playbook ansible/k3s-bringup.yml --tags k3s`, which
reapplies MetalLB, Longhorn, the backup targets, the crons, CoreDNS and the node config, and
arms three gated control-plane tasks. A three-line RBAC edit to
`templates/readonly-rbac.yaml.j2` needed `--tags kubeconfig`, which is what was applied by
hand on 2026-09-22 with ok=15 changed=2 (#2294, #2307).

WHAT THIS DERIVES. Every task file in the role carries its own tags, so a changed file maps
to the tags of the tasks that read it:

  - `tasks/<f>.yml`: the tags on its own tasks.
  - `templates/<f>` or `files/<f>`: the tags of every task file naming `<f>`, following a
    template that another template includes or imports.
  - `defaults/main.yml` or `vars/<f>.yml`: the top-level keys whose value changed, then the
    tags of every task file and template naming one of those keys, following another vars
    key that interpolates one.

A `set_fact` IS FOLLOWED, as a fourth edge. Its mapping keys become host variables that
outlive the task file that set them, so a value derived under one tag and read under another
is real work in both places. `RoleIndex.tags_of` therefore returns a task file's own tags
UNION the tags of every task file and template reading a fact it sets. Until #2384 it did
not, and a change reaching only the setter narrowed to the setter's tags while the consumer's
were dropped — work left merged and unapplied under a marker the operator then clears. Every
setup-role task file that derives a fact AND reaches `tags_of` reads that fact where its own
tags already apply, so the edge widens no answer this tree can produce today.
`test_the_fact_edge_widens_no_real_setup_role_today` is the census that measures it, and
`MEASURED_SETTERS` beside it names the five files it covers.

ANY DOUBT IS A REFUSAL, and the caller falls back to the role tag. A tag that matches nothing
makes Ansible exit 0 having applied nothing — the silent-success failure
`deploy_changes.setup_tags_for` and `deploy_remediation.k8s_remediation` both already guard
against — so a derivation that is not certain must widen rather than narrow. A task file with
no `tags:` of its own is the sharpest case: its tasks inherit from wherever it is imported,
so a hit there says nothing about which tag selects it. A tag must also be REACHABLE in the
playbook the remediation prints: the role has to sit in that playbook's `roles:`, and the task
file has to be statically imported from the role's `tasks/main`.
`tasks/storage_smoke.yml` belongs to `k3s-storage-smoke.yml`, and `k3s-bringup.yml --tags
storage_smoke` selects only `always` tasks.

WHO CALLS IT. `deploy_defer.record` runs it as a SUBPROCESS through
`deploy_narrow.narrow_setup_role`, because this module parses YAML and the deployer's unit
runs under `uv run --no-project`, which cannot import yaml — the boundary
`narrow_deploy_plane` already established for the deploy plane. The tags it returns are
stored in the `manual_plane_tags` marker, so the journal line, the Discord alert, the
SessionStart banner and `land.sh` all quote ONE derivation rather than each repeating it.
`land_tags.confirmed_narrow_tags` also calls `role_tags` in-process, over one PR's own range,
but only as a guard: `land.sh` prints the stored row, and only when it contains that answer.

The READING those rules do — the git reads, the YAML parses and `RoleIndex` — is
`narrow_setup_index.py` beside this. One derivation, split at the 600-line module cap.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import sys

import yaml

from lib import yaml_fast
from lib.git import git
from narrow_setup_index import (
    SETUP_TREE,
    CannotNarrow,
    RoleIndex,
    foreign_tags,
    show_at,
)
from narrow_setup_playbook import playbook_applies_role

# The directories under a role a changed path can be narrowed from. Everything else in the
# role — `handlers/` (whose tasks run under the notifying task's tags), `meta/`, `tests/`,
# a `CLAUDE.md` — refuses, so the caller prints the role tag.
NARROWABLE = ("tasks/", "templates/", "files/", "defaults/", "vars/")


# Paths inside a role that reach no host, so they add no tag requirement: prose Ansible never
# renders, and a role-local `tests/` directory (the no-role-ships-a-test-file invariant is
# `ansible/tests/repo/test_no_role_ships_a_test_file.py`). They are SKIPPED rather than
# refused, because a role `CLAUDE.md` rides along in most real ranges and refusing on one
# would leave the narrowing almost never firing — measured against the k3s role's history,
# where 4 of the 5 most recent ranges carry the role's own CLAUDE.md.
#
# A `.md` under `files/` or `templates/` is NOT prose: a task can copy or render it onto a
# host, so it narrows like any other file there.
def _reaches_no_host(rel: str) -> bool:
    if rel.startswith(("files/", "templates/")):
        return False
    return rel.endswith(".md") or rel.startswith("tests/")


def changed_keys(path: str, old: str, new: str, repo: str) -> set[str]:
    """The top-level keys of a vars file whose value changed between two refs.

    A file either ref does not carry refuses: a `defaults/main.yml` added by the range has no
    before-state to diff, and one deleted by it leaves nothing to narrow to. A key removed
    counts as changed, because the tasks reading it change behaviour.
    """
    before, after = show_at(old, path, repo), show_at(new, path, repo)
    if before is None or after is None:
        raise CannotNarrow(f"{path} is absent at {old if before is None else new}")
    try:
        a = yaml_fast.safe_load(before) or {}
        b = yaml_fast.safe_load(after) or {}
    except yaml.YAMLError as exc:
        raise CannotNarrow(f"{path} does not parse: {exc}") from exc
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise CannotNarrow(f"{path} is not a mapping of keys")
    try:
        return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    except RecursionError as exc:
        # A recursive alias (`k: &l [1, *l]`) loads as a value containing itself, and `!=`
        # recurses into it until the stack runs out.
        raise CannotNarrow(f"{path} holds a recursive alias") from exc


def path_tags(
    rel: str, index: RoleIndex, old: str, new: str, repo: str
) -> frozenset[str]:
    """The tags one changed path below a role reaches, or a refusal."""
    if rel.startswith("tasks/"):
        return index.tags_of(rel)
    if rel.startswith(("templates/", "files/")):
        tags = index.readers_of(rel.rsplit("/", 1)[-1])
        if not tags:
            # Only a template cycle ends here: every reader found was a template already
            # visited. An empty answer is not "needs nothing" — it would drop this path from
            # the range's union without a word.
            raise CannotNarrow(
                f"{rel} reaches no task file, only templates naming each other"
            )
        return tags
    if rel.startswith(("defaults/", "vars/")):
        tags: set[str] = set()
        for key in sorted(changed_keys(f"{index.prefix}{rel}", old, new, repo)):
            got = index.key_readers(key)
            if not got:
                # Per key, not over the union: a key whose only readers are keys naming it
                # back would otherwise drop out silently beside one that did narrow.
                raise CannotNarrow(
                    f"{key} reaches no task file, only vars keys naming each other"
                )
            tags |= got
        if not tags:
            raise CannotNarrow(
                f"{rel} changed no key, so nothing says which tag to run"
            )
        return frozenset(tags)
    raise CannotNarrow(f"{rel} is not in a directory this can narrow from")


def role_tags(
    role: str, role_tag: str, old: str, new: str, repo: str, playbook: str
) -> frozenset[str]:
    """The narrow tags the range `old..new` needs for one setup role, or a refusal.

    Args:
        role: the role directory under `ansible/roles/setup/`.
        role_tag: the `--tags` value selecting the WHOLE role, which the result must not
            contain — a narrowing that lands back on it has narrowed nothing.
        old: the commit the checkout was on.
        new: the commit carrying the change.
        repo: the checkout to read, which is only ever read through `git show`/`ls-tree`, so
            a dirty or already-fast-forwarded working tree does not change the answer.
        playbook: the repo-relative playbook the remediation prints. A role no play in it
            lists under `roles:` refuses: none of the role's own tags reach a host through
            it, and `RoleIndex.reachable` assumes that entry is the path the tags take.

    Raises:
        CannotNarrow: any doubt at all. The caller prints the role tag instead.
    """
    prefix = f"{SETUP_TREE}/{role}/"
    r = git("diff", "--name-only", f"{old}..{new}", "--", prefix, cwd=repo, check=False)
    if r.returncode != 0:
        raise CannotNarrow(
            f"`git diff {old}..{new} -- {prefix}` failed: {r.stderr.strip()}"
        )
    changed = [line for line in r.stdout.splitlines() if line]
    if not changed:
        raise CannotNarrow(f"{old}..{new} changes nothing under {prefix}")
    playbook_text = show_at(new, playbook, repo)
    if not playbook_applies_role(playbook_text, role):
        raise CannotNarrow(f"no play in {playbook} lists {role} under roles:")
    index = RoleIndex(role, new, repo)
    tags: set[str] = set()
    for path in changed:
        rel = path[len(prefix) :]
        if _reaches_no_host(rel):
            continue
        got = path_tags(rel, index, old, new, repo)
        print(f"narrow-setup: {rel} -> {','.join(sorted(got))}", file=sys.stderr)
        tags |= got
    if not tags:
        # Every changed path reaches no host. The deployer should not have deferred this range
        # at all, so there is no narrowing to offer — and an empty `--tags` value runs the
        # whole playbook, which is the opposite of what an empty answer means here.
        raise CannotNarrow("every changed path reaches no host, so no tag applies")
    if role_tag in tags:
        raise CannotNarrow(f"the derivation lands on {role_tag}, the whole-role tag")
    # Read the other roles only once a tag is in hand: `initial_setup.yml` lists fifteen roles
    # and every refusal above returns before paying for them.
    shared = tags & foreign_tags(role, playbook_text, new, repo)
    if shared:
        raise CannotNarrow(
            f"{', '.join(sorted(shared))} is also declared by another role {playbook} "
            "applies, so that tag would run the other role's tasks too"
        )
    return frozenset(tags)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", help="the role directory under ansible/roles/setup/")
    parser.add_argument("old", help="the commit the checkout was on")
    parser.add_argument("new", help="the commit carrying the change")
    parser.add_argument(
        "--role-tag",
        default=None,
        help="the --tags value selecting the whole role (default: the role name)",
    )
    parser.add_argument(
        "--playbook",
        required=True,
        help="the playbook the remediation prints, e.g. ansible/k3s-bringup.yml",
    )
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    try:
        tags = role_tags(
            args.role,
            args.role_tag or args.role,
            args.old,
            args.new,
            args.repo,
            args.playbook,
        )
    except CannotNarrow as exc:
        print(f"narrow-setup: cannot narrow {args.role} ({exc})", file=sys.stderr)
        return 1
    print(",".join(sorted(tags)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
