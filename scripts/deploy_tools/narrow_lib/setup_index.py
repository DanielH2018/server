#!/usr/bin/env python3
"""One setup role's task files, templates and vars, read at a single git ref.

`lib.narrow_git` holds the three primitives this module shares with `narrow_broad` — the
`CannotNarrow` refusal, `show_at` and the YAML mapping parse (#2419).

`narrow_setup` asks the questions — which tags does this changed path reach, and does the
answer narrow anything — and this module holds the reading that answers them: the git reads,
the YAML parses, and `RoleIndex`, which walks the role's own name and variable edges.

The two modules are one derivation, split by the kind of work. `narrow_setup` owns the rules
`role_tags` states in its docstring, and everything here is the reading those rules do (#2371).
The split holds because this half imports nothing from `narrow_setup` and reads none of its
private names. The walks over one parsed task list (its imports, includes, facts and tags) are
in `task_walk` beside this, since they read nothing but the YAML handed to them.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_edges.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/

import posixpath
import re

import yaml

from lib import yaml_fast
from lib.git import git
from lib.narrow_git import CannotNarrow, mapping_at, show_at
from deploy_tools.narrow_lib.setup_playbook import (
    declared_tags,
    play_role_includes,
    playbook_roles,
)
from deploy_tools.narrow_lib.task_walk import (
    _SPECIAL_TAGS,
    _dynamic_includes,
    _set_facts,
    _static_imports,
    _task_tags,
    file_tags,
)

SETUP_TREE = "ansible/roles/setup"


# What a template uses to pull in another template. A change to the inner one reaches every
# task that renders an outer one, so the scan follows that edge instead of stopping at the
# first file.
_TEMPLATE_EDGE = re.compile(r"{%-?\s*(?:import|include|from)\s")


def foreign_tags(role: str, playbook_text: str | None, ref: str, repo: str) -> set[str]:
    """Every tag the OTHER setup roles in this playbook declare.

    A tag two roles declare does not select one of them. `firewall` is carried by tasks in
    both `deploy_ui` and `initial_setup`, so `initial_setup.yml --tags firewall` runs both
    roles' firewall tasks — the printed command would apply a role the change never touched
    (#2350). The derivation refuses such a tag and the caller prints the whole-role tag, which
    is the direction this module always fails in.

    Raises:
        CannotNarrow: another role's task file does not parse, or parses to something other
            than a list of tasks, so which tags it declares is unknown. Unknown is not
            "no collision" — the shape `file_tags` refuses on the changed role's own file is
            refused here too (#2445).
    """
    out: set[str] = set()
    for other in sorted(playbook_roles(playbook_text) - {role}):
        prefix = f"{SETUP_TREE}/{other}/tasks/"
        for path in _tracked(ref, prefix, repo):
            if not _is_role_yaml(path):
                continue
            text = show_at(ref, path, repo)
            if text is None:
                continue
            try:
                doc = yaml_fast.safe_load(text)
            except yaml.YAMLError as exc:
                raise CannotNarrow(f"{path} does not parse: {exc}") from exc
            # A mapping here is unreadable, not empty: `declared_tags` walks a list, so a
            # file of some other shape contributes no tags and a collision it alone declares
            # would be missed — the printed `--tags` would then run the other role's tasks
            # too, which is the #2350 failure this function exists to prevent. Skipping it
            # silently also left the module disagreeing with itself: `file_tags` raises on
            # the same shape (#2445). Reachable since #2437 widened the filter above to every
            # extension Ansible loads, so a `tasks/main.json` holding a mapping gets here.
            if doc is None:
                # An EMPTY document is the one non-list shape that is knowable rather than
                # unreadable: a file with no tasks declares no tags, so it cannot hide a
                # collision. Refusing on it would be a false refusal with the widest possible
                # blast radius — this loop reads every OTHER setup role, so one empty
                # `tasks/*.yml` anywhere in `roles/setup/` would stop narrowing for every
                # setup change. `file_tags` reads one file, the changed role's own, and can
                # afford to refuse.
                continue
            if not isinstance(doc, list):
                raise CannotNarrow(f"{path} is not a list of tasks")
            out |= declared_tags(doc)
    return out


def _scalars(value, seen: set[int]):
    """Every scalar under `value`, as text, nested mapping keys included.

    A mapping key is yielded as well as its value, because a name can sit on either side: a
    `defaults/` structure keyed by a template name is as much a mention as one listing it.
    `seen` holds the id of every container already walked, so a recursive YAML alias
    (`a: &x [*x]`, which the parser resolves into a structure containing itself) terminates.
    """
    if isinstance(value, dict):
        if id(value) in seen:
            return
        seen.add(id(value))
        for key, inner in value.items():
            yield from _scalars(key, seen)
            yield from _scalars(inner, seen)
    elif isinstance(value, (list, tuple, set)):
        if id(value) in seen:
            return
        seen.add(id(value))
        for inner in value:
            yield from _scalars(inner, seen)
    elif value is not None:
        yield str(value)


def _top_level_keys_naming(doc: dict, name: str | re.Pattern) -> set[str]:
    """The top-level keys of a parsed vars file whose value mentions `name`.

    A walk over the PARSED value rather than a scan of the file's lines. The line scan this
    replaced tracked "which key's block does this line sit in", which two ordinary YAML shapes
    break (#2371): a blank line inside a block scalar looks like the end of the key's block, so
    everything after it was attributed to no key at all, and an alias (`demo_b: *a`) copies a
    value without repeating the text that named anything. Both returned a narrower tag list
    than the change needed, which is the one direction this module must never fail in.

    A compiled pattern matches by `search`, for a variable name that must not match inside a
    longer one. The key's own name is searched too, so `key_readers` can subtract the key it
    asked about rather than have it silently absent.
    """
    mentions = (
        name if isinstance(name, re.Pattern) else re.compile(re.escape(name))
    ).search
    return {
        str(key)
        for key, value in doc.items()
        if any(mentions(s) for s in _scalars(key, set()))
        or any(mentions(s) for s in _scalars(value, set()))
    }


# The names Ansible loads from a role's `tasks/`, `defaults/` and `vars/`, read off its own
# source: `Role._load_role_yaml` tries `main` with `.yml`, `.yaml`, `.json` and no extension —
# for `tasks/` as much as for the two vars directories — and `DataLoader._get_dir_vars_files`
# takes the same four from a `main/` directory, skipping hidden names and `~` backups. A README
# or a `.j2` there is not one of them, and parsing it would refuse the whole role over a file
# Ansible never loads either.
#
# The order is Ansible's own precedence for an unnamed `main`, which `_main_task_file` reads
# back: the first extension that exists wins and the rest are dead files.
_LOADED_EXTENSIONS = (".yml", ".yaml", ".json", "")


def _is_role_yaml(rel: str) -> bool:
    """Whether Ansible would load this `tasks/`, `defaults/` or `vars/` file at all.

    Reading only `.yml` and `.yaml` skipped a role's `tasks/main.json` and its extensionless
    `tasks/main`, which `import_tasks` can name too (#2434). A task file the index skips
    contributes no tags, so a template or a vars key it alone reads narrows to fewer tags than
    the change needs — the one direction this module must never fail in.
    """
    name = posixpath.basename(rel)
    if name.startswith(".") or name.endswith("~"):
        return False
    return posixpath.splitext(name)[1] in _LOADED_EXTENSIONS


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
        facts: task-file basename -> the variables it derives with `set_fact`. Those reach
            every task file of the role, so `tags_of` follows them as variable edges.
        template_text: template path below the role -> its text, for the include edges.
        vars_doc: `defaults/` or `vars/` path below the role -> its parsed mapping. Parsed
            once here rather than per lookup, since `key_readers` asks every one of them a
            question on every step of its walk.
        reachable: the task files the role's `tasks/main` reaches through static imports — the
            only ones the role's entry in its playbook runs. A tag read off any other file (one
            a separate playbook imports) can select nothing under the printed command.
        play_included: task file -> the tags of the play task in `playbook_text` that runs it
            with `include_role ... tasks_from:`, or the reason none provably does. Empty
            without a playbook. `k3s-bringup.yml`'s join play reaches `tasks/agent.yml` this
            way under `k3s_agent`, which the remediation prints with `-e join_agent=` (#5628).
        included: task file -> the tags selecting it, for a file a reachable one pulls in
            with `include_tasks`, or the reason no tag provably does (`_dynamic_includes`).
            Several include sites union, each refusal refusing the file.
    """

    def __init__(
        self, role: str, ref: str, repo: str, playbook_text: str | None = None
    ) -> None:
        self.prefix = f"{SETUP_TREE}/{role}/"
        self.tags: dict[str, frozenset[str] | None] = {}
        self.task_text: dict[str, str] = {}
        self.facts: dict[str, frozenset[str]] = {}
        self.template_text: dict[str, str] = {}
        self.vars_doc: dict[str, dict] = {}
        for path in _tracked(ref, self.prefix, repo):
            rel = path[len(self.prefix) :]
            if not rel.startswith(("tasks/", "templates/", "defaults/", "vars/")):
                # `files/` and the rest are matched by NAME only and never read, so a binary
                # file there cannot stop the scans that do read text.
                continue
            text = show_at(ref, path, repo)
            if text is None:
                raise CannotNarrow(f"{path} is listed at {ref} but does not read back")
            if rel.startswith("tasks/") and _is_role_yaml(rel):
                self.task_text[rel] = text
                self.tags[rel] = file_tags(text)
                self.facts[rel] = self._facts_set_by(rel)
            elif rel.startswith("templates/"):
                self.template_text[rel] = text
            elif rel.startswith(("defaults/", "vars/")) and _is_role_yaml(rel):
                # A vars file that does not parse refuses the whole narrowing, the way a task
                # file that does not parse already does: the key walk is what says which tags a
                # changed key reaches, and a file it cannot read is a reader it cannot see.
                self.vars_doc[rel] = mapping_at(text, rel)
        if not self.tags:
            raise CannotNarrow(f"{self.prefix}tasks/ holds no task file at {ref}")
        self.reachable = self._reachable_from(self._main_task_file(ref))
        self.included = self._included_from(self.reachable)
        self.play_included = self._play_included_from(playbook_text, role)

    def _facts_set_by(self, rel: str) -> frozenset[str]:
        """The facts one task file derives, read off its parsed tasks."""
        try:
            doc = yaml_fast.safe_load(self.task_text[rel])
        except yaml.YAMLError as exc:
            raise CannotNarrow(f"{rel} does not parse: {exc}") from exc
        return frozenset(_set_facts(doc if isinstance(doc, list) else []))

    def _main_task_file(self, ref: str) -> str:
        """The `tasks/main` file Ansible would load, by its own extension precedence.

        `Role._load_role_yaml` takes the first of `main.yml`, `main.yaml`, `main.json` and a
        bare `main` that exists, and the rest are dead files. Hard-coding `tasks/main.yml`
        gave a role whose entry carries another extension an EMPTY reachable set, so every
        `tags_of` refused with a message about static imports that did not describe what
        happened (#2434).
        """
        for ext in _LOADED_EXTENSIONS:
            rel = f"tasks/main{ext}"
            if rel in self.task_text:
                return rel
        raise CannotNarrow(f"{self.prefix}tasks/ holds no main file at {ref}")

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

    def _included_from(self, files: frozenset[str]) -> dict[str, frozenset[str] | str]:
        """Every task file `files` include dynamically, and the tags selecting it."""
        out: dict[str, frozenset[str] | str] = {}
        for rel in sorted(files):
            doc = yaml_fast.safe_load(self.task_text[rel])
            for name, answer in _dynamic_includes(doc if isinstance(doc, list) else []):
                target = posixpath.normpath(f"tasks/{name}")
                if target in files:
                    continue
                before = out.get(target, frozenset())
                if isinstance(before, str):
                    continue
                out[target] = answer if isinstance(answer, str) else before | answer
        return out

    def _play_included_from(
        self, playbook_text: str | None, role: str
    ) -> dict[str, frozenset[str] | str]:
        """`play_role_includes` keyed by the task file each `tasks_from:` resolves to.

        A name resolves the way `_main_task_file` resolves `main`: bare, or with one of the
        extensions Ansible loads. Two files answering one name refuse that name rather than
        guess which Ansible picks. Several include sites union, each refusal refusing the file.
        """
        out: dict[str, frozenset[str] | str] = {}
        for name, answer in play_role_includes(playbook_text, role):
            stem = posixpath.normpath(f"tasks/{name}")
            found = [
                stem + ext for ext in _LOADED_EXTENSIONS if stem + ext in self.task_text
            ]
            if len(found) > 1:
                answer = f"tasks_from: {name} names {' and '.join(found)}"
            for rel in found:
                before = out.get(rel, frozenset())
                if not isinstance(before, str):
                    out[rel] = answer if isinstance(answer, str) else before | answer
        return out

    def _play_included_tags(self, rel: str) -> frozenset[str]:
        """The include's tags that every task of a play-included file also carries.

        `include_role` is dynamic: its tags select the include, and the tasks inside then run
        on their own tags alone. So a tag selects all of the file's work only when the include
        AND every task in the file carry it — every task of `agent.yml` carries `k3s_agent`.
        """
        answer = self.play_included[rel]
        if isinstance(answer, str):
            raise CannotNarrow(answer)
        doc = yaml_fast.safe_load(self.task_text[rel])
        per_task = _task_tags(doc if isinstance(doc, list) else [], frozenset())
        if any("never" in t for t in per_task):
            raise CannotNarrow(
                f"{rel} carries the special tag never, which selects opt-in tasks "
                "rather than this change's work"
            )
        tags = (
            answer.intersection(*per_task) - _SPECIAL_TAGS if per_task else frozenset()
        )
        if not tags:
            raise CannotNarrow(
                f"{rel} is included from a play under {', '.join(sorted(answer))}, which not "
                "every task in it carries, so that tag may run none of them"
            )
        return tags

    def tags_of(self, rel: str, seen: frozenset[str] = frozenset()) -> frozenset[str]:
        """The tags selecting one task file; refuses one untagged, unknown or unreached.

        A FACT THIS FILE DERIVES is a variable edge, and its consumers' tags are part of the
        answer (#2384). `set_fact` writes a host variable that outlives the task file, so a
        value derived under one tag and read under another is real work in both places; the
        name scans see it in both files and nothing but this ties the two. Without the edge a
        change reaching only the setter narrowed to the setter's tags and dropped the
        consumer's, which leaves work merged and unapplied under a marker the operator clears.

        `seen` carries the walk `readers_of` and `key_readers` share, so the three recurse
        into each other and still terminate. A setter names its own fact, so `key_readers`
        comes straight back here under a `key:` marker already on the walk and returns empty;
        that empty is NOT the refusal the vars branch raises on, because the frame that put
        the marker there supplies the tags — the same reasoning `key_readers`'s template
        branch states.
        """
        if rel not in self.tags:
            raise CannotNarrow(f"{rel} is not a task file of this role")
        if rel in self.reachable:
            tags = self.tags[rel]
        elif rel in self.included:
            tags = self.included[rel]
            if isinstance(tags, str):
                raise CannotNarrow(tags)
            # Per task, not `self.tags[rel]`: that is None for a file with any untagged task,
            # which is the shape an include exists for. `apply` adds the include's tag to a
            # `never` task in it, so that tag would run the opt-in work.
            doc = yaml_fast.safe_load(self.task_text[rel])
            per_task = _task_tags(doc if isinstance(doc, list) else [], frozenset())
            if any("never" in t for t in per_task):
                raise CannotNarrow(
                    f"{rel} carries the special tag never, which selects opt-in tasks "
                    "rather than this change's work"
                )
        elif rel in self.play_included:
            tags = self._play_included_tags(rel)
        else:
            raise CannotNarrow(
                f"{rel} is neither statically imported from the role's tasks/main file nor "
                "included with tags that select it, from the role or from a play of its "
                "playbook, so no printed command runs it under its own tags"
            )
        if tags is None:
            raise CannotNarrow(
                f"{rel} carries no tags of its own, so its tasks inherit them from wherever "
                "it is imported"
            )
        if "never" in tags:
            raise CannotNarrow(
                f"{rel} carries the special tag never, which selects opt-in tasks rather "
                "than this change's work"
            )
        tags = tags - _SPECIAL_TAGS
        if not tags:
            raise CannotNarrow(
                f"{rel} carries only the special tag always, so no tag names its work"
            )
        for fact in sorted(self.facts.get(rel, frozenset())):
            tags |= self.key_readers(fact, seen)
        return tags

    def readers_of(
        self, name: str, seen: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        """The tags of every task file naming `name`, templates followed one edge at a time.

        `name` is a bare file name, which is how a task's `src:` and a template's `include`
        both name the thing they read. A template that names it is not an answer yet — it is
        the same question asked of that template's own readers.

        THE THREE KINDS OF HIT UNION, they do not shadow each other (#2344). A template can be
        named in a task's `src:` AND in a `defaults/` structure a different task file renders
        from — `k3s_render_stamp_groups` handed to `common/tasks/release_bin.yml` is that
        shape. Stopping at the first kind returned one reader's tags and dropped the other's,
        so the operator cleared the marker over a partly applied change. A `defaults/` key that
        names it but that NOTHING reads still refuses, through `key_readers`: that is the
        fail-closed half, and it is unchanged.
        """
        if name in seen:
            return frozenset()
        seen = seen | {name}
        tags: set[str] = set()
        hit = False
        for rel, text in self.task_text.items():
            if name in text:
                hit = True
                tags |= self.tags_of(rel, seen)
        for rel, text in self.template_text.items():
            outer = rel.rsplit("/", 1)[-1]
            if outer == name or name not in text:
                continue
            if not _TEMPLATE_EDGE.search(text):
                continue
            hit = True
            if outer in seen:
                # Already on the walk, so the recursion that put it there owns its answer.
                continue
            got = self.readers_of(outer, seen)
            if not got:
                raise CannotNarrow(
                    f"{name} reaches no task file through {outer}, only templates naming "
                    "each other"
                )
            tags |= got
        keys = self._vars_keys_naming(name)
        if keys:
            hit = True
            for key in sorted(keys):
                if f"key:{key}" in seen:
                    continue
                got = self.key_readers(key, seen)
                if not got:
                    # A key reaching only a cycle: dropping it narrows to the rest alone.
                    raise CannotNarrow(
                        f"{key} reaches no task file, only names in a cycle"
                    )
                tags |= got
        if not hit:
            raise CannotNarrow(f"nothing in this role names {name}")
        return frozenset(tags)

    def _vars_keys_naming(self, name: str) -> set[str]:
        """The `defaults/` and `vars/` keys whose value names `name`, or an empty set.

        A host script's template is often named in a data structure rather than in a `src:`:
        `setup/k3s` collects them in `k3s_render_stamp_groups` and hands the group to
        `common/tasks/release_bin.yml` through a `vars:` block on the import. The task file
        naming the KEY is the one that renders the template, so its tags are part of the
        answer — wider than the one import site, and still far narrower than the whole role.

        It answers rather than refusing, because `readers_of` unions it with the direct hits
        and an empty answer there is only a refusal when nothing else matched either.
        """
        return {
            key
            for doc in self.vars_doc.values()
            for key in _top_level_keys_naming(doc, name)
        }

    def mentions(self, name: str) -> bool:
        """Whether any task file, template or vars value of the role names `name` as a word."""
        word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        texts = [*self.task_text.values(), *self.template_text.values()]
        return any(word.search(t) for t in texts) or any(
            _top_level_keys_naming(doc, word) for doc in self.vars_doc.values()
        )

    def key_readers(
        self, key: str, seen: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        """The tags of every task file or template reading the variable `key`.

        A hit in a template maps to that template's readers, the same edge `readers_of`
        follows. A key nothing in the role reads refuses: it may be consumed by a `when:` on
        an import — where the tags belong to the import site, not to the file — or by another
        role entirely.

        `seen` is the template names already on the walk, carried down from `readers_of` so
        the two can recurse into each other and still terminate: a template that names a key
        whose only reader is that same template would otherwise loop forever once the vars
        answer stopped being a fallback (#2344).

        ANOTHER `defaults/` OR `vars/` KEY interpolating this one is a reader too, and the
        same question is asked of it: `k3s_node_dns_options` interpolates
        `k3s_node_dns_timeout`, so every reader of the first also reads the second. Keys go on
        `seen` under a `key:` prefix, and a key already there is SKIPPED rather than recursed
        into — the walk that put it there owns its answer.

        A reader whose own answer is empty REFUSES here rather than merging (#2371). `path_tags`
        and `readers_of` guard the outermost call that way, but a cycle one step in returned an
        empty set that this unioned silently: `demo_y` interpolating both the changed key and
        `demo_z`, with `demo_z` naming `demo_y` back, dropped everything `demo_y` reaches and
        left the narrowing reading as complete.
        """
        marker = f"key:{key}"
        if marker in seen:
            return frozenset()
        seen = seen | {marker}
        mention = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
        tags: set[str] = set()
        hit = False
        naming = [_top_level_keys_naming(d, mention) for d in self.vars_doc.values()]
        for other in sorted(set().union(*naming) - {key}):
            hit = True
            if f"key:{other}" in seen:
                continue
            got = self.key_readers(other, seen)
            if not got:
                raise CannotNarrow(
                    f"{key} reaches no task file through {other}, only vars keys naming "
                    "each other"
                )
            tags |= got
        for rel, text in self.task_text.items():
            if mention.search(text):
                hit = True
                tags |= self.tags_of(rel, seen)
        for rel, text in self.template_text.items():
            if mention.search(text):
                hit = True
                # No empty-answer guard here, unlike the key branch above. `readers_of`
                # raises on a branch of its own that reaches nothing. It returns empty only
                # when the name, or every reader it found, is already on the walk, and the
                # frame that put it there supplies those tags, so the union stays complete.
                tags |= self.readers_of(rel.rsplit("/", 1)[-1], seen)
        if not hit:
            raise CannotNarrow(f"nothing in this role reads {key}")
        return frozenset(tags)
