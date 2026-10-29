"""The files one setup role's tasks write on one host, as `probe.py applied` compares them.

`expected(role_dir, host, ctx)` walks the role's `tasks/main.yml` and every task file it
imports, through `block` and `always`, and turns each file-writing task into a `Want`. A
`rescue` is not walked: it runs only when its block fails, so it undoes the block rather than
stating what a clean apply leaves. An import path renders with `role_path` and `playbook_dir`,
so the shared `{{ role_path }}/../common/tasks/` files are walked too, with the import's
`vars:` on top. It reads five modules:

- `template`: the render of `templates/<src>`, with `dest`, `mode`, `owner` and `group`;
- `copy`: the bytes of `files/<src>`, or the render of `content`, with the same fields;
- `file` with `state: absent`: a path that must not exist. `state: directory` checks the mode
  and owner only. Any other `file` state is skipped;
- `cron` with a `cron_file`: `/etc/cron.d/<cron_file>`, present or absent. Ansible writes the
  line itself, so the content is not compared;
- the same five spelled `ansible.builtin.*`.

A `when:` (its own, or a block's) is Jinja over the walk's context: the host's merged vars,
Jinja-valued ones resolved, plus an import's `vars:`. Its names decide whether it can be read.
A name with no static value (a `set_fact` or `register` anywhere in the role, even one that
also has a default, `item`, an `ansible_*` fact, or a name the vars lack) makes the gate
unreadable, whatever test or filter wraps it. A condition of static names only is evaluated,
and one that reads false drops the task.

A `set_fact` the walk reaches is evaluated when it is unlooped, its gate reads true, it is the
role's only setter of each name, and its values read static names only (#6359). From there on
its names are static, with Ansible's native types; `applied_facts` has the rules. An import's
`vars:` evaluate the same way. A template or `copy` content that reads a run-time name is not
compared: its `Want` is `unchecked`, and `applied` prints it UNCHECKED (#6378). The exception
is a `register` whose task reads a file over ssh, the walk's `reads`: when `applied` passes
that file's text in `texts`, the walk reaches the register task with its gate read true, and
the role registers that name once, the name takes the result Ansible would register (#6518).

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

import re
from dataclasses import dataclass
from pathlib import Path

from collections import Counter

from diagnostics.probe_lib.applied_facts import (
    LATE,
    SET_FACTS,
    STUB,
    content_hash,
    expr_names,
    late_names,
    native,
    registered,
    remote_read,
    setters,
)
from jinja2 import StrictUndefined, TemplateSyntaxError
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env
from lib.repo_paths import ANSIBLE

_IMPORTS = ("import_tasks", "include_tasks")
_ROLE_INCLUDES = tuple(
    f"{prefix}{kind}"
    for kind in ("import_role", "include_role")
    for prefix in ("", "ansible.builtin.")
)
_MODULES = ("template", "copy", "file", "cron")
_LOOPS = ("loop", "with_items", "with_list")


@dataclass(frozen=True)
class Want:
    """What one task leaves at one path.

    Attributes:
        path: the absolute path on the host.
        task: the task's name, for the report.
        present: False for an absent arm.
        sha256: the expected content's hash, or None when it is not compared.
        why_no_content: why `sha256` is None, for the report; "" when it is set or moot.
        unchecked: the content reads a run-time name, so it is UNCHECKED, not ok-meta.
        mode: the declared mode as four octal digits, or None when undeclared.
        owner: the declared owner, or None.
        group: the declared group, or None.
    """

    path: str
    task: str
    present: bool = True
    sha256: str | None = None
    why_no_content: str = ""
    unchecked: bool = False
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

    def __init__(self, role_dir: Path, host: str, ctx: dict, texts: dict | None = None):
        self.role_dir, self.host = role_dir, host
        # The text of each (host, path) a register reads over ssh, as `applied` read it.
        self.texts = texts or {}
        self.reads: set[tuple[str, str]] = set()
        # A shared task file names its own templates through these two, as Ansible sets them.
        self.ctx = {"role_path": str(role_dir), "playbook_dir": str(ANSIBLE), **ctx}
        self.wants: list[Want] = []
        self.skips: list[Skip] = []
        # The task file being walked, for an UNCHECKED line.
        self.file = "tasks/main.yml"
        self.text_env = make_ansible_env([role_dir / "templates"])
        self.gate_env = make_ansible_env(undefined_cls=StrictUndefined)
        # Names a `set_fact` or `register` sets during the run, in any of the role's task
        # files, so a gate read before the walk reaches the setter still sees them. A shared
        # file outside the role adds its own as the walk passes it.
        self.setters: Counter = Counter()
        for path in sorted((role_dir / "tasks").rglob("*.yml")):
            self.setters.update(setters(yaml_fast.safe_load(path.read_text())))
        self.run_time = set(self.setters)
        # The values of the set_facts the walk evaluated, which outlive the import that set them.
        self.facts: dict = {}

    def gate(self, whens: list) -> str | None:
        """None when the task runs on the host, `"no"` when it does not, else the reason.

        Readability comes from each condition's NAMES, never from whether evaluating it
        raises: `fact is defined` or `fact | default(false)` would evaluate against the
        static vars and settle a run-time fact on a guess. A condition whose every name is
        static is evaluated; one false condition drops the task, as `when:` list items AND
        together. Otherwise a condition that reads a run-time name makes the gate unreadable.
        """
        if not whens:
            return None
        ns = {**self.ctx, "inventory_hostname": self.host}
        unread: set[str] = set()
        for when in whens:
            try:
                names = expr_names(self.gate_env, "{{ " + str(when) + " }}")
            except TemplateSyntaxError as exc:
                return f"its when: `{when}` does not parse ({exc.message})"
            # DECIDED: a name that is both a default and a set_fact or register is run-time,
            # because the run can overwrite the default before the gate reads it (#6356). A
            # set_fact the walk evaluated is the exception: `learn` has its value (#6359).
            late = late_names(names, ns, self.run_time)
            if late:
                unread |= late
                continue
            try:
                if not self.gate_env.compile_expression(str(when))(**ns):
                    return "no"
            except Exception as exc:
                return f"its when: cannot be read for {self.host} (`{when}`: {exc})"
        if unread:
            return f"when: reads run-time name(s) {', '.join(sorted(unread))}"
        return None

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
        self.run_time |= set(setters([task]))
        self.learn(task, here)
        self.resolve(task, here)
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "always"):
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

    def learn(self, task: dict, whens: list) -> None:
        """Make static each name an evaluable `set_fact` sets; the module docstring has when."""
        args = next((task[k] for k in SET_FACTS if isinstance(task.get(k), dict)), None)
        if (
            args is None
            or any(k in task for k in _LOOPS)
            or self.gate(whens) is not None
        ):
            return
        ns = {**self.ctx, "inventory_hostname": self.host}
        for key, value in args.items():
            if key == "cacheable" or self.setters[str(key)] != 1:
                continue
            ok, out = native(self.gate_env, value, ns, self.run_time)
            if ok:
                self.facts[str(key)] = out
                self.run_time.discard(str(key))
        self.ctx = {**self.ctx, **self.facts}

    def resolve(self, task: dict, whens: list) -> None:
        """Make static a `register` read over ssh, once `texts` holds the file it read."""
        name = task.get("register")
        if not isinstance(name, str) or self.setters[name] != 1:
            return
        if (src := remote_read(self, task)) is None or self.gate(whens) is not None:
            return
        self.reads.add(src)
        # An empty or unread file stays run-time, so its row reads UNCHECKED, never DRIFT.
        if (self.texts.get(src) or "").strip():
            self.facts[name] = registered(self.texts[src])
            self.run_time.discard(name)
            self.ctx = {**self.ctx, **self.facts}

    def loop_items(self, task: dict) -> list | None:
        """The items a task loops over, `[None]` when it does not loop, None when unknown.

        A literal list is read as written. A loop written as a bare `{{ name }}` whose name
        the context holds as a list is read from the context; any other is unknown. Each
        item's Jinja is rendered, as Ansible templates the loop before it binds `item`, so
        `path: "{{ item.path }}"` over `{path: "{{ home }}/.ssh"}` reaches a literal (#6409).
        """
        loop = next((task[k] for k in _LOOPS if k in task), None)
        if loop is None:
            return _no_loop()
        if isinstance(loop, str) and (m := _BARE_NAME.match(loop.strip())):
            loop = self.ctx.get(m.group(1))
        if isinstance(loop, list) and all(isinstance(i, (str, dict)) for i in loop):
            return [self.render_item(i) for i in loop]
        return None

    def render_item(self, value):
        """`value` with each string in it rendered; one that does not render stays as written.

        A string left as written still holds Jinja, so the field render that reads it skips
        the task with its usual reason.
        """
        if isinstance(value, dict):
            return {k: self.render_item(v) for k, v in value.items()}
        if isinstance(value, str):
            rendered = self.render(value)
            return value if rendered is None else rendered
        return value

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
                self.ctx = {**outer, **self.facts}
                if item is not None:
                    self.ctx[loop_var] = item
                ns = {**self.ctx, "inventory_hostname": self.host}
                extra = {}
                for key, value in (task.get("vars") or {}).items():
                    ok, out = native(self.gate_env, value, ns, self.run_time)
                    extra[key] = out if ok else value
                self.ctx = {**self.ctx, **extra}
                self.tasks(path, whens)
        finally:
            self.ctx = {**outer, **self.facts}

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
        sha, why = content_hash(self, module, args, item)
        unchecked = why.startswith(LATE)
        self.wants.append(
            Want(
                dest,
                name,
                sha256=sha,
                why_no_content=why,
                unchecked=unchecked,
                mode=mode,
                owner=owner,
                group=group,
            )
        )


def _shown(path: Path, role_dir: Path) -> str:
    """`path` relative to the role, or to `ansible/` for a shared file outside it."""
    resolved = path.resolve()
    for base in (role_dir.resolve(), ANSIBLE.resolve(), role_dir.resolve().parent):
        if resolved.is_relative_to(base):
            return str(resolved.relative_to(base))
    return str(path)


def _walked(role_dir: Path, host: str, ctx: dict, files, texts) -> _Walk:
    walk = _Walk(role_dir, host, ctx, texts)
    for name in files:
        walk.tasks(role_dir / "tasks" / f"{name}.yml", [])
    return walk


def expected(
    role_dir: Path,
    host: str,
    ctx: dict,
    files: tuple[str, ...] = ("main",),
    texts: dict | None = None,
) -> tuple[list[Want], list[Skip]]:
    """Every path the role writes on `host`, and the file-writing tasks it skipped.

    `files` names the task files a play runs there, under `tasks/` without `.yml`, as
    `applied_hosts.task_files` gives them. `texts` maps each (host, path) in `reads` to the
    file's text.
    """
    walk = _walked(role_dir, host, ctx, files, texts)
    return walk.wants, walk.skips


def reads(
    role_dir: Path, host: str, ctx: dict, files: tuple[str, ...] = ("main",)
) -> set[tuple[str, str]]:
    """The (host, path) of each file a `register` the walk reaches on `host` reads over ssh."""
    return _walked(role_dir, host, ctx, files, None).reads
