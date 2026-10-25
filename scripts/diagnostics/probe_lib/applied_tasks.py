"""The files one setup role's tasks write on one host, as `probe.py applied` compares them.

`expected(role_dir, host, ctx)` walks the role's `tasks/main.yml` and every file it imports with
a literal name, through `block`, `rescue` and `always`, and turns each file-writing task into a
`Want`. It reads five modules:

- `template`: the render of `templates/<src>`, with `dest`, `mode`, `owner` and `group`;
- `copy`: the bytes of `files/<src>`, or the render of `content`, with the same fields;
- `file` with `state: absent`: a path that must not exist. `state: directory` checks the mode
  and owner only. Any other `file` state is skipped;
- `cron` with a `cron_file`: `/etc/cron.d/<cron_file>`, present or absent. Ansible writes the
  line itself, so the content is not compared;
- the same five spelled `ansible.builtin.*`.

A task is SKIPPED, with the reason, rather than guessed at: its `when:` (its own, or a
block's) cannot be read for the host; it loops over a list that is not literal; a field still
holds Jinja or a `STUB` after the render; or it writes from a variable the light render cannot
resolve. A `when:` that reads false for the host drops the task, as Ansible would.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
from dataclasses import dataclass
from pathlib import Path

from deploy_tools.setup_lib.setup_gates import GateUnreadable, eval_when_strict
from lib import yaml_fast
from lib.ansible_jinja_env import make_ansible_env, template_env
from lib.render_guard import render_or_error

STUB = "STUB"
_IMPORTS = ("import_tasks", "include_tasks")
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
    """A file-writing task this probe does not compare, and why."""

    task: str
    reason: str


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


class _Walk:
    """One walk of a role's tasks for one host; `wants` and `skips` collect as it goes."""

    def __init__(self, role_dir: Path, host: str, ctx: dict):
        self.role_dir, self.host, self.ctx = role_dir, host, ctx
        self.defaults = role_dir / "defaults" / "main.yml"
        self.wants: list[Want] = []
        self.skips: list[Skip] = []
        self.text_env = make_ansible_env([role_dir / "templates"])

    def gate(self, whens: list) -> str | None:
        """None when the task runs on the host, `"no"` when it does not, else the reason."""
        if not whens:
            return None
        try:
            runs = eval_when_strict(whens, self.host, role_defaults=self.defaults)
        except GateUnreadable as exc:
            return f"its when: cannot be read for {self.host} ({exc})"
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
        for task in yaml_fast.safe_load(path.read_text()) or []:
            if isinstance(task, dict):
                self.task(task, whens)

    def task(self, task: dict, whens: list) -> None:
        own = task.get("when")
        here = [*whens, *(own if isinstance(own, list) else [own] if own else [])]
        name = str(task.get("name") or "unnamed task")
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                for sub in task.get(key) or []:
                    if isinstance(sub, dict):
                        self.task(sub, here)
            return
        target = _import_of(task)
        if target is not None:
            if "{{" not in target and (self.role_dir / "tasks" / target).is_file():
                if (why := self.gate(here)) is None:
                    self.tasks(self.role_dir / "tasks" / target, here)
                elif why != "no":
                    self.skips.append(Skip(f"import {target}", why))
            return
        found = _module(task)
        if found is None:
            return
        if (why := self.gate(here)) is not None:
            if why != "no":
                self.skips.append(Skip(name, why))
            return
        loop = next((task[k] for k in _LOOPS if k in task), None)
        if loop is None:
            self.one(name, *found, item=None)
        elif isinstance(loop, list) and all(isinstance(i, (str, dict)) for i in loop):
            for item in loop:
                self.one(name, *found, item=item)
        else:
            self.skips.append(Skip(name, "it loops over a list that is not literal"))

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


def expected(role_dir: Path, host: str, ctx: dict) -> tuple[list[Want], list[Skip]]:
    """Every path the role writes on `host`, and the file-writing tasks it skipped."""
    walk = _Walk(role_dir, host, ctx)
    walk.tasks(role_dir / "tasks" / "main.yml", [])
    return walk.wants, walk.skips
