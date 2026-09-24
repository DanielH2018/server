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
    tags of every task file and template naming one of those keys.

ANY DOUBT IS A REFUSAL, and the caller falls back to the role tag. A tag that matches nothing
makes Ansible exit 0 having applied nothing — the silent-success failure
`deploy_changes.setup_tags_for` and `deploy_remediation.k8s_remediation` both already guard
against — so a derivation that is not certain must widen rather than narrow. A task file with
no `tags:` of its own is the sharpest case: its tasks inherit from wherever it is imported,
so a hit there says nothing about which tag selects it.

WHO CALLS IT. `deploy_defer.record` runs it as a SUBPROCESS through
`deploy_narrow.narrow_setup_role`, because this module parses YAML and the deployer's unit
runs under `uv run --no-project`, which cannot import yaml — the boundary
`narrow_deploy_plane` already established for the deploy plane. The tags it returns are
stored in the `manual_plane_tags` marker, so the journal line, the Discord alert, the
SessionStart banner and `land.sh` all quote ONE derivation rather than each repeating it.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import re
import sys

import yaml

from lib import yaml_fast
from lib.git import git

SETUP_TREE = "ansible/roles/setup"

# The directories under a role a changed path can be narrowed from. Everything else in the
# role — `handlers/` (whose tasks run under the notifying task's tags), `meta/`, `tests/`,
# a `CLAUDE.md` — refuses, so the caller prints the role tag.
NARROWABLE = ("tasks/", "templates/", "files/", "defaults/", "vars/")

# What a template uses to pull in another template. A change to the inner one reaches every
# task that renders an outer one, so the scan follows that edge instead of stopping at the
# first file.
_TEMPLATE_EDGE = re.compile(r"{%-?\s*(?:import|include|from)\s")


class CannotNarrow(Exception):
    """This change reaches something no narrow tag list covers. The caller uses the role tag."""


def _show(ref: str, path: str, repo: str) -> str | None:
    """The file's text at `ref`, or None when the ref does not carry it."""
    r = git("show", f"{ref}:{path}", cwd=repo, check=False)
    return r.stdout if r.returncode == 0 else None


def _task_tags(tasks, inherited: frozenset[str]) -> list[frozenset[str]]:
    """The effective tag set of every task in a task list, blocks walked through.

    A `block`/`rescue`/`always` mapping is not a task: its own `tags:` apply to the tasks
    inside it, which is why the walk carries `inherited` down rather than counting the block
    itself. An empty frozenset in the result means a task no tag of this file selects.
    """
    out: list[frozenset[str]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            raise CannotNarrow("a task list holds something that is not a mapping")
        own = task.get("tags") or []
        own = [own] if isinstance(own, str) else own
        effective = inherited | frozenset(str(t) for t in own)
        nested = [
            task.get(key) for key in ("block", "rescue", "always") if task.get(key)
        ]
        if nested:
            for inner in nested:
                out += _task_tags(inner, effective)
        else:
            out.append(effective)
    return out


def file_tags(text: str) -> frozenset[str] | None:
    """The tags that select every task in one task file, or None when one is untagged.

    None is a refusal, and it is the common case for a file nobody tagged: `main.yml`'s
    imports, `unit-logging.yml`, `longhorn-weekly-shard.yml`. Those inherit their tags from
    the import site, so a hit in one says nothing about which tag applies it.
    """
    try:
        doc = yaml_fast.safe_load(text)
    except yaml.YAMLError as exc:
        raise CannotNarrow(f"a task file does not parse: {exc}") from exc
    if not isinstance(doc, list):
        raise CannotNarrow("a task file is not a list of tasks")
    per_task = _task_tags(doc, frozenset())
    if not per_task or any(not tags for tags in per_task):
        return None
    return frozenset().union(*per_task)


def _tracked(ref: str, prefix: str, repo: str) -> list[str]:
    """Every tracked path under `prefix` at `ref`."""
    r = git("ls-tree", "-r", "--name-only", ref, "--", prefix, cwd=repo, check=False)
    if r.returncode != 0:
        raise CannotNarrow(f"`git ls-tree {prefix}` failed: {r.stderr.strip()}")
    return [line for line in r.stdout.splitlines() if line]


class RoleIndex:
    """One setup role's task files and templates, as read at a single ref.

    Attributes:
        tags: task-file basename -> the tags selecting it, or None when it is untagged.
        task_text: task-file basename -> its text, for the name scans.
        template_text: template path below the role -> its text, for the include edges.
    """

    def __init__(self, role: str, ref: str, repo: str) -> None:
        self.prefix = f"{SETUP_TREE}/{role}/"
        self.tags: dict[str, frozenset[str] | None] = {}
        self.task_text: dict[str, str] = {}
        self.template_text: dict[str, str] = {}
        for path in _tracked(ref, self.prefix, repo):
            rel = path[len(self.prefix) :]
            text = _show(ref, path, repo)
            if text is None:
                raise CannotNarrow(f"{path} is listed at {ref} but does not read back")
            if rel.startswith("tasks/") and rel.endswith((".yml", ".yaml")):
                self.task_text[rel] = text
                self.tags[rel] = file_tags(text)
            elif rel.startswith("templates/"):
                self.template_text[rel] = text
        if not self.tags:
            raise CannotNarrow(f"{self.prefix}tasks/ holds no task file at {ref}")

    def tags_of(self, rel: str) -> frozenset[str]:
        """The tags selecting one task file, refusing when it is untagged or unknown."""
        if rel not in self.tags:
            raise CannotNarrow(f"{rel} is not a task file of this role")
        tags = self.tags[rel]
        if tags is None:
            raise CannotNarrow(
                f"{rel} carries no tags of its own, so its tasks inherit them from wherever "
                "it is imported"
            )
        return tags

    def readers_of(
        self, name: str, seen: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        """The tags of every task file naming `name`, templates followed one edge at a time.

        `name` is a bare file name, which is how a task's `src:` and a template's `include`
        both name the thing they read. A template that names it is not an answer yet — it is
        the same question asked of that template's own readers.
        """
        if name in seen:
            return frozenset()
        seen = seen | {name}
        tags: set[str] = set()
        hit = False
        for rel, text in self.task_text.items():
            if name in text:
                hit = True
                tags |= self.tags_of(rel)
        for rel, text in self.template_text.items():
            if rel.rsplit("/", 1)[-1] == name or name not in text:
                continue
            if not _TEMPLATE_EDGE.search(text):
                continue
            hit = True
            tags |= self.readers_of(rel.rsplit("/", 1)[-1], seen)
        if not hit:
            raise CannotNarrow(f"no task file or template of this role names {name}")
        return frozenset(tags)

    def key_readers(self, key: str) -> frozenset[str]:
        """The tags of every task file or template reading the variable `key`.

        A hit in a template maps to that template's readers, the same edge `readers_of`
        follows. A key nothing in the role reads refuses: it may be consumed by a `when:` on
        an import — where the tags belong to the import site, not to the file — or by another
        role entirely.
        """
        mention = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
        tags: set[str] = set()
        hit = False
        for rel, text in self.task_text.items():
            if mention.search(text):
                hit = True
                tags |= self.tags_of(rel)
        for rel, text in self.template_text.items():
            if mention.search(text):
                hit = True
                tags |= self.readers_of(rel.rsplit("/", 1)[-1])
        if not hit:
            raise CannotNarrow(f"nothing in this role reads {key}")
        return frozenset(tags)


def changed_keys(path: str, old: str, new: str, repo: str) -> set[str]:
    """The top-level keys of a vars file whose value changed between two refs.

    A file either ref does not carry refuses: a `defaults/main.yml` added by the range has no
    before-state to diff, and one deleted by it leaves nothing to narrow to. A key removed
    counts as changed, because the tasks reading it change behaviour.
    """
    before, after = _show(old, path, repo), _show(new, path, repo)
    if before is None or after is None:
        raise CannotNarrow(f"{path} is absent at {old if before is None else new}")
    try:
        a = yaml_fast.safe_load(before) or {}
        b = yaml_fast.safe_load(after) or {}
    except yaml.YAMLError as exc:
        raise CannotNarrow(f"{path} does not parse: {exc}") from exc
    if not isinstance(a, dict) or not isinstance(b, dict):
        raise CannotNarrow(f"{path} is not a mapping of keys")
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}


def path_tags(
    rel: str, index: RoleIndex, old: str, new: str, repo: str
) -> frozenset[str]:
    """The tags one changed path below a role reaches, or a refusal."""
    if rel.startswith("tasks/"):
        return index.tags_of(rel)
    if rel.startswith(("templates/", "files/")):
        return index.readers_of(rel.rsplit("/", 1)[-1])
    if rel.startswith(("defaults/", "vars/")):
        tags: set[str] = set()
        for key in sorted(changed_keys(f"{index.prefix}{rel}", old, new, repo)):
            tags |= index.key_readers(key)
        if not tags:
            raise CannotNarrow(
                f"{rel} changed no key, so nothing says which tag to run"
            )
        return frozenset(tags)
    raise CannotNarrow(f"{rel} is not in a directory this can narrow from")


def role_tags(
    role: str, role_tag: str, old: str, new: str, repo: str
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
    index = RoleIndex(role, new, repo)
    tags: set[str] = set()
    for path in changed:
        rel = path[len(prefix) :]
        got = path_tags(rel, index, old, new, repo)
        print(f"narrow-setup: {rel} -> {','.join(sorted(got))}", file=sys.stderr)
        tags |= got
    if role_tag in tags:
        raise CannotNarrow(f"the derivation lands on {role_tag}, the whole-role tag")
    if not tags:
        raise CannotNarrow("the derivation names no tag")
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
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    try:
        tags = role_tags(
            args.role, args.role_tag or args.role, args.old, args.new, args.repo
        )
    except CannotNarrow as exc:
        print(f"narrow-setup: cannot narrow {args.role} ({exc})", file=sys.stderr)
        return 1
    print(",".join(sorted(tags)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
