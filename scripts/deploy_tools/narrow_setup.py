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
so a hit there says nothing about which tag selects it. A tag must also be REACHABLE in the
playbook the remediation prints: the role has to sit in that playbook's `roles:`, and the task
file has to be statically imported from `tasks/main.yml`. `tasks/storage_smoke.yml` belongs to
`k3s-storage-smoke.yml`, and `k3s-bringup.yml --tags storage_smoke` selects only `always`
tasks.

WHO CALLS IT. `deploy_defer.record` runs it as a SUBPROCESS through
`deploy_narrow.narrow_setup_role`, because this module parses YAML and the deployer's unit
runs under `uv run --no-project`, which cannot import yaml — the boundary
`narrow_deploy_plane` already established for the deploy plane. The tags it returns are
stored in the `manual_plane_tags` marker, so the journal line, the Discord alert, the
SessionStart banner and `land.sh` all quote ONE derivation rather than each repeating it.
`land_tags.confirmed_narrow_tags` also calls `role_tags` in-process, over one PR's own range,
but only as a guard: `land.sh` prints the stored row, and only when it contains that answer.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
import posixpath
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


# What a template uses to pull in another template. A change to the inner one reaches every
# task that renders an outer one, so the scan follows that edge instead of stopping at the
# first file.
_TEMPLATE_EDGE = re.compile(r"{%-?\s*(?:import|include|from)\s")


class CannotNarrow(Exception):
    """This change reaches something no narrow tag list covers. The caller uses the role tag."""


def _show(ref: str, path: str, repo: str) -> str | None:
    """The file's text at `ref`, or None when the ref does not carry it.

    A file that is not UTF-8 text refuses rather than raising: the scans below read text, and
    a traceback in the deployer's journal is a worse way to say "cannot narrow" than this.
    """
    try:
        r = git("show", f"{ref}:{path}", cwd=repo, check=False)
    except UnicodeDecodeError as exc:
        raise CannotNarrow(f"{path} is not text at {ref}") from exc
    return r.stdout if r.returncode == 0 else None


# The spellings of a STATIC task import. `include_tasks` is deliberately absent: a dynamic
# include's tasks run only when the include task itself is selected, and the tags on the
# included file's own tasks do not select the include — so a hit in such a file names a tag
# that may run nothing.
_STATIC_IMPORTS = (
    "import_tasks",
    "ansible.builtin.import_tasks",
    "ansible.legacy.import_tasks",
)


def _static_imports(tasks) -> list[str]:
    """The literal file names a task list imports statically, blocks walked through.

    A templated name (`{{ role_path }}/...`) is left out: which file it names is only known at
    run time, so a file reached only that way is not provably reachable.
    """
    out: list[str] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out += _static_imports(task[key])
        for key in _STATIC_IMPORTS:
            value = task.get(key)
            if isinstance(value, dict):
                value = value.get("file")
            if isinstance(value, str) and "{{" not in value:
                out.append(value)
    return out


def _playbook_applies_role(text: str | None, role: str) -> bool:
    """Whether a playbook lists `role` in some play's `roles:`, the path its role tag takes."""
    if text is None:
        return False
    try:
        plays = yaml_fast.safe_load(text)
    except yaml.YAMLError:
        return False
    for play in plays if isinstance(plays, list) else []:
        if not isinstance(play, dict):
            continue
        for entry in play.get("roles") or []:
            if isinstance(entry, dict):
                entry = entry.get("role") or entry.get("name")
            if entry == role:
                return True
    return False


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


def _top_level_keys_naming(text: str, name: str) -> set[str]:
    """The top-level keys of a vars file whose block of lines mentions `name`.

    A line scan rather than a parse, because the value may be a nested structure and what is
    wanted is only "which key's block holds this string". A top-level key is a line starting in
    column zero with a `key:`; everything indented under it belongs to that key.
    """
    keys: set[str] = set()
    current = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w]*)\s*:", line)
        if m:
            current = m.group(1)
        elif not line.strip() or line.lstrip().startswith("#"):
            if not line.strip():
                current = None
            continue
        if current and name in line:
            keys.add(current)
    return keys


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
        reachable: the task files `tasks/main.yml` reaches through static imports — the only
            ones the role's entry in its playbook runs. A tag read off any other file (one a
            separate playbook imports, one only a play-level `include_role` with
            `tasks_from:` reaches) can select nothing under the printed command.
    """

    def __init__(self, role: str, ref: str, repo: str) -> None:
        self.prefix = f"{SETUP_TREE}/{role}/"
        self.tags: dict[str, frozenset[str] | None] = {}
        self.task_text: dict[str, str] = {}
        self.template_text: dict[str, str] = {}
        self.vars_text: dict[str, str] = {}
        for path in _tracked(ref, self.prefix, repo):
            rel = path[len(self.prefix) :]
            if not rel.startswith(("tasks/", "templates/", "defaults/", "vars/")):
                # `files/` and the rest are matched by NAME only and never read, so a binary
                # file there cannot stop the scans that do read text.
                continue
            text = _show(ref, path, repo)
            if text is None:
                raise CannotNarrow(f"{path} is listed at {ref} but does not read back")
            if rel.startswith("tasks/") and rel.endswith((".yml", ".yaml")):
                self.task_text[rel] = text
                self.tags[rel] = file_tags(text)
            elif rel.startswith("templates/"):
                self.template_text[rel] = text
            elif rel.startswith(("defaults/", "vars/")):
                self.vars_text[rel] = text
        if not self.tags:
            raise CannotNarrow(f"{self.prefix}tasks/ holds no task file at {ref}")
        self.reachable = self._reachable_from("tasks/main.yml")

    def _reachable_from(self, start: str) -> frozenset[str]:
        """Every task file `start` imports statically, itself included, transitively."""
        seen: set[str] = set()
        todo = [start]
        while todo:
            rel = todo.pop()
            if rel in seen or rel not in self.task_text:
                continue
            seen.add(rel)
            try:
                doc = yaml_fast.safe_load(self.task_text[rel])
            except yaml.YAMLError as exc:
                raise CannotNarrow(f"{rel} does not parse: {exc}") from exc
            for name in _static_imports(doc if isinstance(doc, list) else []):
                todo.append(posixpath.normpath(f"tasks/{name}"))
        return frozenset(seen)

    def tags_of(self, rel: str) -> frozenset[str]:
        """The tags selecting one task file; refuses one untagged, unknown or unreached."""
        if rel not in self.tags:
            raise CannotNarrow(f"{rel} is not a task file of this role")
        if rel not in self.reachable:
            raise CannotNarrow(
                f"{rel} is not statically imported from tasks/main.yml, so the role's entry "
                "in its playbook never runs it under its own tags"
            )
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
        if hit:
            return frozenset(tags)
        return self._named_in_vars(name)

    def _named_in_vars(self, name: str) -> frozenset[str]:
        """The tags of whatever reads the `defaults/` key whose value names `name`.

        A host script's template is often named in a data structure rather than in a `src:`:
        `setup/k3s` collects them in `k3s_render_stamp_groups` and hands the group to
        `common/tasks/release_bin.yml` through a `vars:` block on the import. The task file
        naming the KEY is the one that renders the template, so its tags are the answer — wider
        than the one import site, and still far narrower than the whole role.
        """
        keys = {
            key
            for rel, text in self.vars_text.items()
            for key in _top_level_keys_naming(text, name)
        }
        if not keys:
            raise CannotNarrow(f"nothing in this role names {name}")
        tags: set[str] = set()
        for key in sorted(keys):
            tags |= self.key_readers(key)
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
            tags |= index.key_readers(key)
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
    if not _playbook_applies_role(_show(new, playbook, repo), role):
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
    if role_tag in tags:
        raise CannotNarrow(f"the derivation lands on {role_tag}, the whole-role tag")
    if not tags:
        # Every changed path reaches no host. The deployer should not have deferred this range
        # at all, so there is no narrowing to offer — and an empty `--tags` value runs the
        # whole playbook, which is the opposite of what an empty answer means here.
        raise CannotNarrow("every changed path reaches no host, so no tag applies")
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
