"""Which tasks and variables carry a secret value, for the secret-key task census.

`derived_variables` reads role defaults and inventory vars. `role_derived_facts` reads the
`set_fact` and `register:` tasks of every role beside one role, including a fact parsed out of a
secret-bearing `lookup('template', ...)`. `_tasks` walks a task file with whether a guard covers
each task.
"""

import functools
import json
import re
from collections.abc import Iterator
from pathlib import Path

from lib import yaml_fast

from _role_census import role_dirs, role_task_files

_STUB_GUARD = re.compile(
    r"\bnot\s*\(?\s*(?:k8s_no_mutate|k8s_dry_run|homelab_secret_stubs)\b"
)


def _word_pattern(names: set[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b")


# A value references a variable only inside a Jinja expression. A bare word does not count:
# headlamp's scopes string "profile,email,groups" holds the secret key name `email`.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def derived_variables(secret_keys: set[str], var_files: list[Path]) -> set[str]:
    """The variables in `var_files` whose value references a secret key, transitively."""
    values: dict[str, str] = {}
    for path in var_files:
        for name, value in (yaml_fast.safe_load(path.read_text()) or {}).items():
            expressions = " ".join(_JINJA.findall(json.dumps(value)))
            values[str(name)] = values.get(str(name), "") + " " + expressions
    derived: set[str] = set()
    while True:
        pattern = _word_pattern(secret_keys | derived)
        found = {
            name
            for name, text in values.items()
            if name not in secret_keys and name not in derived and pattern.search(text)
        }
        if not found:
            return derived
        derived |= found


def _when_text(task: dict) -> str:
    when = task.get("when", "")
    return " and ".join(map(str, when)) if isinstance(when, list) else str(when)


def _tasks(
    tasks: object, guarded: bool = False, guard: re.Pattern[str] = _STUB_GUARD
) -> Iterator[tuple[dict, bool]]:
    """Each task with whether it or an enclosing block carries `guard` on its `when:`."""
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict):
            continue
        here = guarded or bool(guard.search(_when_text(task)))
        yield task, here
        for key in ("block", "rescue", "always"):
            yield from _tasks(task.get(key), here, guard)


def _body(task: dict) -> str:
    """The task's text minus its name, its `register:` and its nested tasks.

    Nested tasks are checked on their own. A register name is what the task writes, not what it
    reads, so a task that reuses a derived register's name is not selected for that alone.
    """
    rest = {
        k: v
        for k, v in task.items()
        if k not in ("name", "register", "block", "rescue", "always")
    }
    return json.dumps(rest, default=str)


_SET_FACT = {"set_fact", "ansible.builtin.set_fact"}
# Keys whose value Ansible evaluates as a Jinja expression without `{{ }}`.
_BARE_JINJA_KEYS = ("when", "until", "changed_when", "failed_when")
_TEMPLATE_LOOKUP = re.compile(r"""lookup\(\s*['"]template['"]\s*,([^)]*)\)""")
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_OTHER_ROLE_TEMPLATE = re.compile(r"roles/k8s/([^/]+)/templates/(.+)")


def _looked_up_templates(role: Path, text: str) -> Iterator[Path]:
    """The templates a `lookup('template', ...)` in `text` reads, resolved against `role`.

    A path is the last quoted string in the call, so `playbook_dir + '/roles/k8s/<r>/...'`
    resolves too; it is read under `<r>`'s own templates.
    """
    for call in _TEMPLATE_LOOKUP.finditer(text):
        quoted = _QUOTED.findall(call.group(1))
        if not quoted:
            continue
        other = _OTHER_ROLE_TEMPLATE.search(quoted[-1])
        yield (
            role.parent / other.group(1) / "templates" / other.group(2)
            if other
            else role / "templates" / quoted[-1]
        )


def role_derived_facts(role: Path, names: re.Pattern[str]) -> set[str]:
    """The fact and register names that carry a secret value into `role`, transitively.

    A `set_fact` key or a `register:` name is derived when its task's Jinja names a match of
    `names` or an already derived name, or when a `set_fact` looks up a role template whose own
    Jinja does. A register's task is read in its `{{ }}` expressions and its bare-Jinja keys
    (`when:`, `until:`, `changed_when:`, `failed_when:`), never in plain text: pihole's gravity
    SQL holds the word `domain`, a secret key's name. Both are play-scoped, so every role beside
    `role` is read too: a fact one role sets is read in another, and a role reads the register
    of a role it includes (#4862).
    """
    return set(_tree_derived(role.parent, _tree_signature(role.parent), names))


def _tree_signature(roles_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Each task and template file under `roles_dir` with its mtime and size.

    The census asks once per role about the same tree, so the result is cached on this rather
    than on the directory alone: an edited, added or removed file misses the cache.
    """
    files = [
        path
        for role in role_dirs(roles_dir)
        for path in [*role_task_files(role), *sorted((role / "templates").rglob("*"))]
        if path.is_file()
    ]
    return tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files)


@functools.cache
def _tree_derived(
    roles_dir: Path,
    _signature: tuple[tuple[str, int, int], ...],
    names: re.Pattern[str],
) -> frozenset[str]:
    # (names the task writes, the text it reads, the templates it looks up)
    writes: list[tuple[set[str], str, list[Path]]] = []
    for sibling in role_dirs(roles_dir):
        for task_file in role_task_files(sibling):
            for task, _ in _tasks(yaml_fast.safe_load(task_file.read_text())):
                # `_body` is JSON, which escapes a double quote; the lookup pattern reads it bare.
                text = " ".join(_JINJA.findall(_body(task))).replace('\\"', '"')
                if isinstance(task.get("register"), str):
                    bare = json.dumps(
                        [task.get(k) for k in _BARE_JINJA_KEYS], default=str
                    )
                    writes.append(({task["register"]}, f"{text} {bare}", []))
                module = next((k for k in _SET_FACT if k in task), None)
                if module is None or not isinstance(task[module], dict):
                    continue
                keys = {str(k) for k in task[module] if k != "cacheable"}
                writes.append((keys, text, list(_looked_up_templates(sibling, text))))
    templates = {
        path: " ".join(_JINJA.findall(path.read_text()))
        for _, _, paths in writes
        for path in paths
        if path.is_file()
    }
    derived: set[str] = set()
    while True:
        secret = (
            re.compile(f"{names.pattern}|{_word_pattern(derived).pattern}")
            if derived
            else names
        )
        # Recomputed each pass: a template naming a fact derived last pass is secret-bearing now.
        secret_templates = {p for p, text in templates.items() if secret.search(text)}
        found = {
            key
            for keys, text, paths in writes
            if not keys <= derived
            and (secret.search(text) or secret_templates.intersection(paths))
            for key in keys
        } - derived
        if not found:
            return frozenset(derived)
        derived |= found
