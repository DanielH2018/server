#!/usr/bin/env python3
"""One setup role's task files, templates and vars, read at a single git ref.

`narrow_setup` asks the questions — which tags does this changed path reach, and does the
answer narrow anything — and this module holds the reading that answers them: the git reads,
the YAML parses, and `RoleIndex`, which walks the role's own name and variable edges.

Split out of `narrow_setup.py` when the parse rewrite (#2371) took that module past the
600-line cap. The two are one derivation: `narrow_setup` owns the rules `role_tags` states in
its docstring, and everything here is what those rules read.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_edges.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import posixpath
import re

import yaml

from lib import yaml_fast
from lib.git import git
from narrow_setup_playbook import declared_tags, playbook_roles

SETUP_TREE = "ansible/roles/setup"


# What a template uses to pull in another template. A change to the inner one reaches every
# task that renders an outer one, so the scan follows that edge instead of stopping at the
# first file.
_TEMPLATE_EDGE = re.compile(r"{%-?\s*(?:import|include|from)\s")


# Ansible's two tags that do not select a subset. `--tags never` selects exactly the tasks
# opted out of every ordinary run, and `--tags always` selects the tasks that run whatever is
# asked for — so neither describes "the work this change needs". `docker_install`'s
# `tasks/install.yml` carries both `never` and `docker-engine-upgrade`, and printing that pair
# would tell an operator to run the engine upgrade a config edit never asked for (#2350).
_SPECIAL_TAGS = frozenset({"never", "always"})


class CannotNarrow(Exception):
    """This change reaches something no narrow tag list covers. The caller uses the role tag."""


def show_at(ref: str, path: str, repo: str) -> str | None:
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


# The spellings of `set_fact`. Its mapping keys become host variables that survive the task
# file that set them, so a task file elsewhere in the role can read one under a different tag.
_SET_FACTS = (
    "set_fact",
    "ansible.builtin.set_fact",
    "ansible.legacy.set_fact",
)

# `set_fact`'s own option, not a fact it sets.
_SET_FACT_OPTIONS = frozenset({"cacheable"})


def _set_facts(tasks) -> set[str]:
    """Every variable name a task list derives with `set_fact`, blocks walked through."""
    out: set[str] = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out |= _set_facts(task[key])
        for key in _SET_FACTS:
            value = task.get(key)
            if isinstance(value, dict):
                out |= {str(k) for k in value} - _SET_FACT_OPTIONS
    return out


def foreign_tags(role: str, playbook_text: str | None, ref: str, repo: str) -> set[str]:
    """Every tag the OTHER setup roles in this playbook declare.

    A tag two roles declare does not select one of them. `firewall` is carried by tasks in
    both `deploy_ui` and `initial_setup`, so `initial_setup.yml --tags firewall` runs both
    roles' firewall tasks — the printed command would apply a role the change never touched
    (#2350). The derivation refuses such a tag and the caller prints the whole-role tag, which
    is the direction this module always fails in.

    Raises:
        CannotNarrow: another role's task file does not parse, so which tags it declares is
            unknown. Unknown is not "no collision".
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
            if isinstance(doc, list):
                out |= declared_tags(doc)
    return out


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
            a separate playbook imports, one only a play-level `include_role` with
            `tasks_from:` reaches) can select nothing under the printed command.
    """

    def __init__(self, role: str, ref: str, repo: str) -> None:
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
                self.vars_doc[rel] = self._vars_mapping(rel, text)
        if not self.tags:
            raise CannotNarrow(f"{self.prefix}tasks/ holds no task file at {ref}")
        self.reachable = self._reachable_from(self._main_task_file(ref))

    @staticmethod
    def _vars_mapping(rel: str, text: str) -> dict:
        """One `defaults/` or `vars/` file as a mapping, or a refusal.

        A file that does not parse refuses the whole narrowing, the way a task file that does
        not parse already does: the key walk is what says which tags a changed key reaches,
        and a file it cannot read is a reader it cannot see.
        """
        try:
            doc = yaml_fast.safe_load(text)
        except yaml.YAMLError as exc:
            raise CannotNarrow(f"{rel} does not parse: {exc}") from exc
        if doc is None:
            return {}
        if not isinstance(doc, dict):
            raise CannotNarrow(f"{rel} is not a mapping of keys")
        return doc

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
        if rel not in self.reachable:
            raise CannotNarrow(
                f"{rel} is not statically imported from the role's tasks/main file, so the "
                "role's entry in its playbook never runs it under its own tags"
            )
        tags = self.tags[rel]
        if tags is None:
            raise CannotNarrow(
                f"{rel} carries no tags of its own, so its tasks inherit them from wherever "
                "it is imported"
            )
        special = tags & _SPECIAL_TAGS
        if special:
            raise CannotNarrow(
                f"{rel} carries the special tag {', '.join(sorted(special))}, which selects "
                "opt-in or unconditional tasks rather than this change's work"
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
