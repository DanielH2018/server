"""The files one setup role's tasks write on one host, as `probe.py applied` compares them.

`expected(role_dir, host, ctx)` walks the role's `tasks/main.yml` and every task file it
imports, through `block`, `rescue` and `always`, and turns each file-writing task into a
`Want`. An import path renders with `role_path` and `playbook_dir`, so the shared
`{{ role_path }}/../common/tasks/` files are walked too, with the import's `vars:` on top. It reads five modules:

- `template`: the render of `templates/<src>`, with `dest`, `mode`, `owner` and `group`;
- `copy`: the bytes of `files/<src>`, or the render of `content`, with the same fields;
- `file` with `state: absent`: a path that must not exist. `state: directory` checks the mode
  and owner only. Any other `file` state is skipped;
- `cron` with a `cron_file`: `/etc/cron.d/<cron_file>`, present or absent. Ansible writes the
  line itself, so the content is not compared;
- the same five spelled `ansible.builtin.*`.

A `when:` (its own, or a block's) is Jinja over the walk's context: the host's merged vars,
Jinja-valued ones resolved, plus an import's `vars:`. A name with no value before the run (a
`set_fact` or `register` the walk has passed, `item`, an `ansible_*` fact) makes the gate
unreadable only if evaluating it reaches that name. A gate that reads false drops the task.

A task is SKIPPED, with the reason, when its `when:` cannot be read, its loop is not literal,
a field still holds Jinja or a `STUB` after the render, or it includes a role. A task FILE the
walk cannot read is UNCHECKED: an import whose path does not render to a file, or whose `when:`
or loop cannot be read. It may install anything, so `applied` exits 75 for it. An import whose
`when:` is false is listed as not applicable. A `copy` with `remote_src` is compared by
metadata only, because its source is on the host.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import StrictUndefined, TemplateSyntaxError, Undefined, meta
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env, template_env
from lib.render_guard import render_or_error
from lib.repo_paths import ANSIBLE

STUB = "STUB"
_IMPORTS = ("import_tasks", "include_tasks")
_ROLE_INCLUDES = tuple(
    f"{prefix}{kind}"
    for kind in ("import_role", "include_role")
    for prefix in ("", "ansible.builtin.")
)
_MODULES = ("template", "copy", "file", "cron")
_LOOPS = ("loop", "with_items", "with_list")
_SET_FACTS = ("set_fact", "ansible.builtin.set_fact", "ansible.legacy.set_fact")
# Names Ansible fills in only while the play runs. Every `ansible_*` name is one too.
_RUN_TIME = frozenset({"item", "hostvars", "groups", "group_names", "play_hosts"})


class _Unreadable(Exception):
    """A gate used a value the walk does not have; the message says which."""


class _Unknown:
    """Stands in for a gate name with no value before the run, and raises when it is used.

    Not a Jinja `Undefined`, so `is defined` cannot call it False. A gate that never reaches
    it, such as `false and fact.rc == 0`, still evaluates, as Ansible's `and` short-circuits.
    """

    def __init__(self, why: str):
        self.why = why

    def _use(self, *_args, **_kwargs):
        raise _Unreadable(self.why)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        self._use()

    __bool__ = __len__ = __iter__ = __contains__ = __getitem__ = __hash__ = _use
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __str__ = __int__ = _use
    __add__ = __radd__ = __call__ = _use


def _defined_test(want: bool):
    def test(value) -> bool:
        if isinstance(value, _Unknown):
            value._use()
        return isinstance(value, Undefined) != want

    return test


@dataclass(frozen=True)
class Want:
    """What one task leaves at one path.

    Attributes:
        path: the absolute path on the host.
        task: the task's name, for the report.
        present: False for an absent arm.
        sha256: the expected content's hash, or None when it is not compared.
        why_no_content: why `sha256` is None, for the report; "" when it is set or moot.
        mode: the declared mode as four octal digits, or None when undeclared.
        owner: the declared owner, or None.
        group: the declared group, or None.
    """

    path: str
    task: str
    present: bool = True
    sha256: str | None = None
    why_no_content: str = ""
    mode: str | None = None
    owner: str | None = None
    group: str | None = None


@dataclass(frozen=True)
class Skip:
    """A file-writing task this probe does not compare, and why.

    `unchecked` marks a task file the walk could not read, because its path, its `when:` or
    its loop does not resolve, so every task in it went unread: `applied` reports it as
    UNCHECKED and its verdict as partial coverage.
    """

    task: str
    reason: str
    unchecked: bool = False


def octal_mode(value) -> str | None:
    """A declared mode as four octal digits; None when undeclared or symbolic (`u=rw`).

    YAML 1.1 reads an unquoted `0644` as the integer 420, so an int is already the value.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value:04o}"
    try:
        return f"{int(str(value).strip(), 8):04o}"
    except ValueError:
        return None


def _module(task: dict) -> tuple[str, dict] | None:
    for name in _MODULES:
        for key in (name, f"ansible.builtin.{name}", f"ansible.legacy.{name}"):
            args = task.get(key)
            if isinstance(args, dict):
                return name, args
    return None


def _import_of(task: dict) -> str | None:
    for name in _IMPORTS:
        for key in (name, f"ansible.builtin.{name}"):
            target = task.get(key)
            if isinstance(target, dict):
                target = target.get("file")
            if isinstance(target, str):
                return target
    return None


_BARE_NAME = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _no_loop() -> list:
    return [None]


class _Walk:
    """One walk of a role's tasks for one host; `wants` and `skips` collect as it goes."""

    def __init__(self, role_dir: Path, host: str, ctx: dict):
        self.role_dir, self.host = role_dir, host
        # A shared task file names its own templates through these two, as Ansible sets them.
        self.ctx = {"role_path": str(role_dir), "playbook_dir": str(ANSIBLE), **ctx}
        self.wants: list[Want] = []
        self.skips: list[Skip] = []
        # The task file being walked, for an UNCHECKED line.
        self.file = "tasks/main.yml"
        self.text_env = make_ansible_env([role_dir / "templates"])
        self.gate_env = make_ansible_env(undefined_cls=StrictUndefined)
        self.gate_env.tests["defined"] = _defined_test(True)
        self.gate_env.tests["undefined"] = _defined_test(False)
        # Names a `set_fact` or `register` the walk has passed sets during the run.
        self.run_time: set[str] = set()

    def gate(self, whens: list) -> str | None:
        """None when the task runs on the host, `"no"` when it does not, else the reason.

        The `when:` is Jinja over the walk's context. A name with no resolved value is an
        `_Unknown`, so the gate is unreadable only when evaluating it reaches that name.
        """
        if not whens:
            return None
        expr = " and ".join(f"({w})" for w in whens)
        unreadable = f"its when: cannot be read for {self.host}"
        ns = {**self.ctx, "inventory_hostname": self.host}
        try:
            names = meta.find_undeclared_variables(
                self.gate_env.parse("{{ " + expr + " }}")
            )
        except TemplateSyntaxError as exc:
            return f"{unreadable} (`{expr}` does not parse: {exc.message})"
        run_time = _RUN_TIME | self.run_time
        for name in names:
            if name in run_time or name.startswith("ansible_") or name not in ns:
                ns[name] = _Unknown(f"`{name}` has no value before the run")
            elif any(m in str(ns[name]) for m in ("{{", STUB)):
                ns[name] = _Unknown(f"`{name}` does not resolve before the run")
        try:
            runs = bool(self.gate_env.compile_expression(expr)(**ns))
        except _Unreadable as exc:
            return f"{unreadable} ({exc})"
        except Exception as exc:
            return f"{unreadable} (`{expr}`: {exc})"
        return None if runs else "no"

    def render(self, value, item=None) -> str | None:
        """`value` rendered with the role's context; None when a STUB or Jinja survives."""
        if value is None:
            return None
        text = str(value)
        if "{{" in text or "{%" in text:
            ctx = dict(self.ctx, item=item) if item is not None else self.ctx
            try:
                text = self.text_env.from_string(text).render(**ctx)
            except Exception:
                return None
        return None if STUB in text or "{{" in text else text

    def tasks(self, path: Path, whens: list) -> None:
        outer, self.file = self.file, _shown(path, self.role_dir)
        try:
            for task in yaml_fast.safe_load(path.read_text()) or []:
                if isinstance(task, dict):
                    self.task(task, whens)
        finally:
            self.file = outer

    def task(self, task: dict, whens: list) -> None:
        own = task.get("when")
        here = [*whens, *(own if isinstance(own, list) else [own] if own else [])]
        name = str(task.get("name") or "unnamed task")
        if isinstance(task.get("register"), str):
            self.run_time.add(task["register"])
        for key in _SET_FACTS:
            if isinstance(task.get(key), dict):
                self.run_time |= {str(k) for k in task[key]}
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                for sub in task.get(key) or []:
                    if isinstance(sub, dict):
                        self.task(sub, here)
            return
        if any(k in task for k in _ROLE_INCLUDES):
            self.skips.append(
                Skip(name, "it includes another role, which this does not follow")
            )
            return
        target = _import_of(task)
        if target is not None:
            self.follow(task, target, here)
            return
        found = _module(task)
        if found is None:
            return
        if (why := self.gate(here)) is not None:
            if why != "no":
                self.skips.append(Skip(name, why))
            return
        items = self.loop_items(task)
        if items is None:
            self.skips.append(Skip(name, "it loops over a list that is not literal"))
            return
        for item in items:
            self.one(name, *found, item=item)

    def loop_items(self, task: dict) -> list | None:
        """The items a task loops over, `[None]` when it does not loop, None when unknown.

        A literal list is read as written. A loop written as a bare `{{ name }}` whose name
        the context holds as a list is read from the context; any other is unknown.
        """
        loop = next((task[k] for k in _LOOPS if k in task), None)
        if loop is None:
            return _no_loop()
        if isinstance(loop, str) and (m := _BARE_NAME.match(loop.strip())):
            loop = self.ctx.get(m.group(1))
        if isinstance(loop, list) and all(isinstance(i, (str, dict)) for i in loop):
            return loop
        return None

    def follow(self, task: dict, target: str, whens: list) -> None:
        """Walk an imported task file with the import's `vars:` on top, or record a skip.

        A shared file is reached as `{{ role_path }}/../common/tasks/<file>.yml` and takes
        its inputs from `vars:`, so both are rendered before the file is walked.
        """
        label = f"import {target}"
        resolved = self.render(target)
        path = None if resolved is None else self.role_dir / "tasks" / resolved
        if path is None or not path.is_file():
            # DECIDED: an import the walk cannot find is UNCHECKED, never a quiet skip. Every
            # task in it went unread, so a role reading clean would claim coverage it lacks.
            why = (
                f"{target!r} does not render to a literal path"
                if resolved is None
                else f"{target!r} renders to {resolved!r}, which is not a file"
            )
            self.skips.append(Skip(f"{self.file}: {label}", why, unchecked=True))
            return
        # DECIDED: a task file whose gate or loop the walk cannot read is UNCHECKED too (#6356).
        # Its tasks install files nobody compared, the defect an unfound import already reports.
        why = self.gate(whens)
        if why == "no":
            why = f"its when: is false for {self.host}, so it does not apply"
            self.skips.append(Skip(label, why))
            return
        if why is not None:
            self.skips.append(Skip(f"{self.file}: {label}", why, unchecked=True))
            return
        items = self.loop_items(task)
        if items is None:
            why = "it loops over a list that is not literal"
            self.skips.append(Skip(f"{self.file}: {label}", why, unchecked=True))
            return
        loop_var = (task.get("loop_control") or {}).get("loop_var", "item")
        outer = self.ctx
        try:
            for item in items:
                self.ctx = outer if item is None else {**outer, loop_var: item}
                extra = {}
                for key, value in (task.get("vars") or {}).items():
                    rendered = self.render(value) if isinstance(value, str) else None
                    extra[key] = rendered if rendered is not None else value
                self.ctx = {**self.ctx, **extra}
                self.tasks(path, whens)
        finally:
            self.ctx = outer

    def one(self, name: str, module: str, args: dict, item) -> None:
        state = args.get("state", "present")
        if module == "cron":
            if not args.get("cron_file"):
                return  # A user crontab, which this does not read.
            dest = self.render(f"/etc/cron.d/{args['cron_file']}", item)
            mode = owner = group = None
        else:
            dest = self.render(args.get("dest") or args.get("path"), item)
            # An int is YAML's reading of an unquoted octal; rendered, 420 would read back
            # as 0420.
            raw = args.get("mode")
            mode = octal_mode(raw if isinstance(raw, int) else self.render(raw, item))
            owner = self.render(args.get("owner"), item)
            group = self.render(args.get("group"), item)
        if dest is None:
            self.skips.append(Skip(name, "its path does not render to a literal"))
            return
        if state == "absent":
            self.wants.append(Want(dest, name, present=False))
            return
        if module == "cron":
            self.wants.append(Want(dest, name, why_no_content="cron writes the line"))
            return
        if module == "file":
            if state == "directory":
                self.wants.append(
                    Want(
                        dest,
                        name,
                        why_no_content="a directory",
                        mode=mode,
                        owner=owner,
                        group=group,
                    )
                )
            else:
                self.skips.append(Skip(name, f"file state {state!r}"))
            return
        sha, why = self.content(module, args, item)
        self.wants.append(
            Want(
                dest,
                name,
                sha256=sha,
                why_no_content=why,
                mode=mode,
                owner=owner,
                group=group,
            )
        )

    def content(self, module: str, args: dict, item) -> tuple[str | None, str]:
        """The expected content's sha256, or None and the reason it is not compared."""
        if module == "copy" and "content" in args:
            text = self.render(args["content"], item)
            if text is None:
                return None, "the render left a STUB or Jinja"
            return hashlib.sha256(text.encode()).hexdigest(), ""
        src = self.render(args.get("src"), item)
        if src is None or src.endswith("/"):
            return None, "its src does not render to one file"
        if module == "copy" and args.get("remote_src") in (True, "yes", "true", "True"):
            return None, "remote_src copies a file already on the host"
        if module == "copy":
            source = self.role_dir / "files" / src
            if not source.is_file():
                return None, f"files/{src} is not in the role"
            return hashlib.sha256(source.read_bytes()).hexdigest(), ""
        if not (self.role_dir / "templates" / src).is_file():
            return None, f"templates/{src} is not in the role"
        rendered, err = render_or_error(
            template_env((self.role_dir / "templates" / src).parent),
            Path(src).name,
            dict(self.ctx, item=item) if item is not None else self.ctx,
        )
        if rendered is None:
            return None, f"the template does not render here ({err})"
        if STUB in rendered:
            return (
                None,
                "the render left a STUB (a secret, or a value only a play resolves)",
            )
        return hashlib.sha256(rendered.encode()).hexdigest(), ""


def _shown(path: Path, role_dir: Path) -> str:
    """`path` relative to the role, or to `ansible/` for a shared file outside it."""
    resolved = path.resolve()
    for base in (role_dir.resolve(), ANSIBLE.resolve(), role_dir.resolve().parent):
        if resolved.is_relative_to(base):
            return str(resolved.relative_to(base))
    return str(path)


def expected(role_dir: Path, host: str, ctx: dict) -> tuple[list[Want], list[Skip]]:
    """Every path the role writes on `host`, and the file-writing tasks it skipped."""
    walk = _Walk(role_dir, host, ctx)
    walk.tasks(role_dir / "tasks" / "main.yml", [])
    return walk.wants, walk.skips
