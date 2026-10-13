"""Which tasks and variables carry a secret value, for the secret-key task census.

`derived_variables` reads role defaults and inventory vars; `role_derived_facts` reads one role's
`set_fact` tasks, including a fact parsed out of a secret-bearing `lookup('template', ...)`.
`_tasks` walks a task file with whether a guard covers each task.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

from lib import yaml_fast

from _role_census import role_task_files

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
    """The task's text minus its name and its nested tasks, which are checked on their own."""
    rest = {
        k: v for k, v in task.items() if k not in ("name", "block", "rescue", "always")
    }
    return json.dumps(rest, default=str)


_SET_FACT = {"set_fact", "ansible.builtin.set_fact"}
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
    """The `set_fact` keys in `role` that carry a secret value, transitively.

    A fact is derived when its task's Jinja names a match of `names` or an already derived fact,
    or when the task looks up a role template whose own Jinja does.
    """
    facts: list[tuple[set[str], str, list[Path]]] = []
    for task_file in role_task_files(role):
        for task, _ in _tasks(yaml_fast.safe_load(task_file.read_text())):
            module = next((k for k in _SET_FACT if k in task), None)
            if module is None or not isinstance(task[module], dict):
                continue
            keys = {str(k) for k in task[module] if k != "cacheable"}
            text = " ".join(_JINJA.findall(_body(task)))
            facts.append((keys, text, list(_looked_up_templates(role, text))))
    secret_templates = {
        path
        for _, _, paths in facts
        for path in paths
        if path.is_file() and names.search(" ".join(_JINJA.findall(path.read_text())))
    }
    derived: set[str] = set()
    while True:
        local = _word_pattern(derived) if derived else None
        found = {
            key
            for keys, text, paths in facts
            if not keys <= derived
            and (
                names.search(text)
                or (local and local.search(text))
                or secret_templates.intersection(paths)
            )
            for key in keys
        } - derived
        if not found:
            return derived
        derived |= found
