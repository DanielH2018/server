"""Which names `probe.py applied`'s walk can read statically, and the values it reads for them.

`applied_tasks` walks a setup role's tasks against the host's static vars. Three things it
reads are Jinja over those vars, and this module decides each one by the expression's NAMES,
never by whether a render happens to succeed:

- `late_names`: the names in an expression that only the run sets. A `set_fact` or
  `register` the walk could not evaluate, `item` and the other magic vars, an `ansible_*` fact,
  and a name whose value still holds Jinja are late. A gate also counts a name the vars lack
  and a `STUB` value as late; a template renders those as `STUB`, which reads as a secret.
- `native`: a `set_fact` value or an include's `vars:` value, evaluated the way Ansible does.
  A value that is one `{{ expr }}` returns the expression's native type, so a list stays a
  list and `{{ x == 'present' }}` stays a bool rather than the truthy string `"False"`.
  `lookup('vars', name)` reads `name` from the vars, or `STUB` when they lack it, since the
  one name a role looks up that way is a secret the static vars never hold.
- `template_late`: the late names a template's own text reads (#6378). A file rendered from
  one has content nobody can predict before the run, so `content_hash` gives no hash and a
  reason starting `LATE`, and `applied` reports the file UNCHECKED.

One kind of `register` is not late once `applied` has read a file: a `command` whose argv is
`ssh [-o OPT]... HOST cat PATH`. `remote_read` names its HOST and PATH, `applied` reads PATH on
HOST, and `registered` gives the walk the result Ansible would register (#6518).
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import re
from collections import Counter
from pathlib import Path

from jinja2 import Environment, Undefined, meta
from lib.ansible_jinja_env import template_env
from lib.render_guard import render_or_error

STUB = "STUB"
# Names Ansible fills in only while the play runs. Every `ansible_*` name is one too, except
# those the static context sets on purpose.
RUN_TIME = frozenset({"item", "hostvars", "groups", "group_names", "play_hosts"})
# `ansible_managed` is a config string, not a fact: ansible-core renders its default, the
# value `setup_templates.ANSIBLE_RUNTIME_CONTEXT` supplies.
STATIC_ANSIBLE = frozenset({"ansible_managed"})
_LOOKUPS = frozenset({"lookup", "query", "q"})
SET_FACTS = ("set_fact", "ansible.builtin.set_fact", "ansible.legacy.set_fact")
COMMANDS = ("command", "ansible.builtin.command", "ansible.legacy.command")
# The reason `content_hash` gives for content it cannot predict, an unchecked `Want` (#6378).
LATE = "it reads run-time name(s)"
_SINGLE = re.compile(r"^\s*\{\{((?:(?!\{\{|\}\}).)*)\}\}\s*$", re.S)


def setters(tasks) -> Counter:
    """How many `register:` and `set_fact` tasks set each name in `tasks`, through blocks."""
    names: Counter = Counter()
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        if isinstance(task.get("register"), str):
            names[task["register"]] += 1
        for key in SET_FACTS:
            if isinstance(task.get(key), dict):
                names.update(str(k) for k in task[key] if k != "cacheable")
        for key in ("block", "rescue", "always"):
            names.update(setters(task.get(key)))
    return names


def remote_read(walk, task: dict) -> tuple[str, str] | None:
    """The (host, path) a task reads with `ssh [-o OPT]... HOST cat PATH`, else None.

    Both render with the walk's context, and None means one did not render to a literal.
    """
    args = next((task[k] for k in COMMANDS if isinstance(task.get(k), dict)), None)
    argv = (args or {}).get("argv")
    if not isinstance(argv, list) or len(argv) < 4 or argv[0] != "ssh":
        return None
    opts, (host, cat, path) = argv[1:-3], argv[-3:]
    if cat != "cat" or opts[::2] != ["-o"] * (len(opts) // 2) or len(opts) % 2:
        return None
    host, path = walk.render(host), walk.render(path)
    return None if host is None or path is None else (host, path)


def registered(text: str) -> dict:
    """What a `command` that printed `text` and exited 0 registers.

    The command module strips the trailing newline from stdout, as `strip_empty_ends` does.
    """
    out = text.rstrip("\r\n")
    return {"stdout": out, "stdout_lines": out.splitlines(), "rc": 0, "failed": False}


def expr_names(env: Environment, text: str) -> set[str]:
    """The undeclared names in Jinja `text`, less the env's globals other than the lookups.

    Raises:
        jinja2.TemplateSyntaxError: `text` does not parse.
    """
    names = meta.find_undeclared_variables(env.parse(text))
    return names - (env.globals.keys() - _LOOKUPS)


def late_names(
    names: set[str], ns: dict, run_time: set[str], *, missing_is_late: bool = True
) -> set[str]:
    """The names among `names` the static vars in `ns` cannot answer for.

    `missing_is_late` is False for a template, which renders a name the vars lack, or one
    holding a `STUB`, as `STUB`: a secret, reported by metadata rather than as UNCHECKED.
    """
    late = set()
    for n in names:
        if n in run_time:
            late.add(n)
        # A template renders a magic var the static context supplies, such as `hostvars`
        # with every host's inventory vars, as the run does. A gate keeps it late (#6356).
        elif n in RUN_TIME and (missing_is_late or n not in ns):
            late.add(n)
        elif n.startswith("ansible_") and n not in STATIC_ANSIBLE:
            late.add(n)
        elif n not in ns:
            if missing_is_late:
                late.add(n)
        elif "{{" in str(ns[n]) or (missing_is_late and STUB in str(ns[n])):
            late.add(n)
    return late


def _vars_lookup(ns: dict):
    def lookup(kind, name, default=None, **_kwargs):
        if kind != "vars":
            raise ValueError(f"lookup({kind!r}) has no static value")
        return ns.get(name, STUB)

    return lookup


def native(env: Environment, value, ns: dict, run_time: set[str]):
    """Evaluate a `set_fact` or include `vars:` value against `ns`, as Ansible does.

    Returns:
        `(True, value)`, or `(False, None)` when the value reads a late name or does not
        evaluate. A list or dict is evaluated member by member.
    """
    if isinstance(value, list):
        pairs = [native(env, v, ns, run_time) for v in value]
        ok = all(p[0] for p in pairs)
        return (True, [p[1] for p in pairs]) if ok else (False, None)
    if isinstance(value, dict):
        pairs = {k: native(env, v, ns, run_time) for k, v in value.items()}
        ok = all(p[0] for p in pairs.values())
        return (True, {k: p[1] for k, p in pairs.items()}) if ok else (False, None)
    if not isinstance(value, str) or ("{{" not in value and "{%" not in value):
        return True, value
    try:
        if late_names(expr_names(env, value) - {"lookup"}, ns, run_time):
            return False, None
        call = {**ns, "lookup": _vars_lookup(ns)}
        if m := _SINGLE.match(value):
            out = env.compile_expression(m.group(1), undefined_to_none=False)(**call)
        else:
            out = env.from_string(value).render(**call)
    except Exception:
        return False, None
    return (False, None) if isinstance(out, Undefined) else (True, out)


def template_late(
    env: Environment, source: str, ns: dict, run_time: set[str]
) -> set[str]:
    """The late names template `source` reads itself.

    A name only a macro it imports reads is not seen. A template that does not parse reads
    none, and its render reports why.
    """
    try:
        names = expr_names(env, source) - _LOOKUPS
    except Exception:
        return set()
    return late_names(names, ns, run_time, missing_is_late=False)


def _late_content(walk, module: str, args: dict, item) -> str | None:
    """Why the content is unknown until the run, or None when the walk can render it."""
    source = args.get("content") if module == "copy" else None
    src = walk.render(args.get("src"), item) if module == "template" else None
    if src and (walk.role_dir / "templates" / src).is_file():
        source = (walk.role_dir / "templates" / src).read_text()
    if not isinstance(source, str):
        return None
    ns = {**walk.ctx, "inventory_hostname": walk.host}
    late = template_late(walk.text_env, source, ns, walk.run_time)
    late -= {"item"} if item is not None else set()
    return f"{LATE} {', '.join(sorted(late))}" if late else None


def content_hash(walk, module: str, args: dict, item) -> tuple[str | None, str]:
    """The sha256 of what a `template` or `copy` task writes, as `applied_tasks._Walk` sees it.

    Returns:
        The hash and "", or None and the reason the content is not compared. The reason
        starts with `LATE` when the content reads a run-time name.
    """
    if (late := _late_content(walk, module, args, item)) is not None:
        return None, late
    if module == "copy" and "content" in args:
        text = walk.render(args["content"], item)
        if text is None:
            return None, "the render left a STUB or Jinja"
        return hashlib.sha256(text.encode()).hexdigest(), ""
    src = walk.render(args.get("src"), item)
    if src is None or src.endswith("/"):
        return None, "its src does not render to one file"
    if module == "copy" and args.get("remote_src") in (True, "yes", "true", "True"):
        return None, "remote_src copies a file already on the host"
    if module == "copy":
        source = walk.role_dir / "files" / src
        if not source.is_file():
            return None, f"files/{src} is not in the role"
        return hashlib.sha256(source.read_bytes()).hexdigest(), ""
    if not (walk.role_dir / "templates" / src).is_file():
        return None, f"templates/{src} is not in the role"
    rendered, err = render_or_error(
        template_env((walk.role_dir / "templates" / src).parent),
        Path(src).name,
        dict(walk.ctx, item=item) if item is not None else walk.ctx,
    )
    if rendered is None:
        return None, f"the template does not render here ({err})"
    if STUB in rendered:
        return (
            None,
            "the render left a STUB (a secret, or a value only a play resolves)",
        )
    return hashlib.sha256(rendered.encode()).hexdigest(), ""
