#!/usr/bin/env python3
"""Whether a role's render digest covers everything a range changed for it (#4781).

THE QUESTION. The GitOps deployer's `k8s_unapplied` discharge accepts a matching render as
proof that a change is live (`deploy_release.render_proof`). The proof covers the bytes
`manifests_digest` and `secret_digest` hash, and nothing else. For a role that acts outside
them, `digest_provable.py` answers "no", and before #4781 that answer kept the line until the
role was deployed for some other reason. PR #4753 added asserts to `deploy.yml` and moved no
rendered byte, and 35 such lines stayed for hours with nothing left to apply.

This asks the narrower question per range: did anything in `BASE..HEAD` that could have
written a line for ROLE change what ROLE does outside its digest? BASE is the commit ROLE was
last applied at (its release record) and HEAD the commit of the matching render, so the range
is everything the digest has to vouch for. The caller takes the render proof only when the
answer is "covered".

THE RULE, FAIL-CLOSED. A changed path is first asked whether it could write a line for ROLE at
all, by the deployer's own mapping (`deploy_changes.services_from_changed_paths`): a path under
ROLE's own directory, or a deploy-plane path when ROLE is a declared service, which is what
`deploy_broad_k8s.fold_deferred` writes lines for. Any other path writes another role's line,
or none, and is skipped. A path that can is covered when one of these holds:

  * it is prose or test material no playbook applies;
  * it is a YAML file Ansible parses (a play file, or a role's `tasks/`, `defaults/`, `vars/`,
    `handlers/` or `meta/`) whose parsed value did not change: a comment or layout edit;
  * it is a Jinja template whose token stream, less its comments, did not change
    (`narrow_lib/templates.py:comment_only`): the render is byte-identical;
  * it is a play file (`deploy.yml`, `pre_tasks/`, `tasks/`, `post_tasks/`) that differs only
    by `assert`, `fail` and `debug` tasks with no `register` or `notify`, plus `vars:` keys only
    those tasks read. Such a task changes nothing: it passes, or it stops the run;
  * it is a filter plugin in `ORDER_ONLY_FILTERS` whose change is confined to the filters
    that only order the play, and to the helpers no other filter reaches;
  * it is an inventory file, a shared template or another filter plugin that the deploy-plane
    narrowing (`narrow_lib/broad.py:broad_path_tags`) maps to tags other than ROLE. That
    derivation greps ROLE's whole tree, `tasks/` included.

Anything else, any refusal and any unreadable file keeps the line. So does a BASE that is not
an ancestor of HEAD.

WHO CALLS IT. `deploy_narrow.digest_covers`, as a subprocess for the reason
`digest_provable.py` is one: this parses YAML and Jinja, and the deployer runs under
`uv run --no-project`. It prints one JSON object, item to bool, and one stderr line per item
it refuses, naming the path that kept it.

Usage: digest_covers.py [--repo PATH] ROLE:BASE:HEAD [ROLE:BASE:HEAD ...]
"""

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import yaml_fast
from lib.cli_help import ArgumentParser
from lib.git import git
from lib.narrow_git import CannotNarrow, show_at

from deploy_tools import digest_provable
from deploy_tools.narrow_lib import broad, filters, paths
from deploy_tools.narrow_lib import templates as narrow_templates

# `broad` put the deployer's `files/` on the path, so there is one path mapper, not two.
from deployer_lib.deploy_changes import (
    _BROAD_PLAY_PREFIXES,
    _is_test_only_path,
    role_of,
    services_from_changed_paths,
)
from gitops_markers import DEPLOY_PLAYBOOK

# Modules whose task changes nothing when it passes and stops the run when it fails. Narrower
# than `digest_provable.PURE_MODULES`: a `set_fact` in a play file sets what later tasks read,
# and `meta` can end the play or flush handlers.
INERT_MODULES = frozenset({"assert", "fail", "debug"})
# Keywords that hand an inert task's result to another task, which makes it not inert.
_PASSES_ON = ("register", "notify")
_TASK_LISTS = ("pre_tasks", "tasks", "post_tasks", "handlers")
_BLOCK_KEYS = ("block", "rescue", "always")
# The role subdirectories Ansible parses as YAML. A file under `files/` or `templates/` is
# shipped or rendered as text, so a YAML comment there is part of what lands.
_PARSED_ROLE_DIRS = frozenset({"tasks", "defaults", "vars", "handlers", "meta"})

# DECIDED: `toposort.py`'s two ordering filters reach no role's own tasks (#4781). `deploy.yml`
# reads `build_k8s_dep_map` only into `_k8s_container_deps_map`, which it reads only as
# `toposort_containers`' argument, whose result is the entries it was given, reordered.
# `tests/test_digest_covers.py::test_the_play_reads_the_ordering_filters_only_to_order_itself`
# pins the first two and `ansible/tests/deploy/test_toposort.py`'s
# `test_returns_original_objects` the third. So a change confined to those two filters and
# their own helpers moves the order roles deploy in, never what a role does. The gap
# taken is a role whose bytes depend on list order; the render proof the caller still
# requires catches that one. `filter_by_platform` in the same file decides WHICH entries
# deploy, so a change reaching it keeps the line.
ORDER_ONLY_FILTERS = {
    "ansible/filter_plugins/toposort.py": frozenset(
        {"build_k8s_dep_map", "toposort_containers"}
    ),
}


def _module(task) -> str | None:
    """The one module a task dict names, without its collection prefix, or None."""
    if not isinstance(task, dict):
        return None
    keys = [k for k in task if k not in digest_provable.TASK_KEYWORDS]
    return keys[0].rsplit(".", 1)[-1] if len(keys) == 1 else None


def _inert(task) -> bool:
    """Whether `task` is an assert, fail or debug that hands nothing to another task."""
    return _module(task) in INERT_MODULES and not any(k in task for k in _PASSES_ON)


def _strip_tasks(tasks):
    """`tasks` less every inert task, recursing into blocks."""
    if not isinstance(tasks, list):
        return tasks
    kept = []
    for task in tasks:
        if _inert(task):
            continue
        if isinstance(task, dict) and "block" in task:
            task = {
                k: _strip_tasks(v) if k in _BLOCK_KEYS else v for k, v in task.items()
            }
        kept.append(task)
    return kept


def _strip(path: str, doc):
    """A play file's parsed value less its inert tasks. `deploy.yml` is a list of plays."""
    if path != DEPLOY_PLAYBOOK:
        return _strip_tasks(doc)
    if not isinstance(doc, list):
        return doc
    return [
        {k: _strip_tasks(v) if k in _TASK_LISTS else v for k, v in play.items()}
        if isinstance(play, dict)
        else play
        for play in doc
    ]


def _var_defs(node, out: Counter) -> Counter:
    """Every `(name, value)` a `vars:` mapping anywhere in `node` defines, counted."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "vars" and isinstance(v, dict):
                for name, value in v.items():
                    out[(name, json.dumps(value, sort_keys=True, default=str))] += 1
            _var_defs(v, out)
    elif isinstance(node, list):
        for item in node:
            _var_defs(item, out)
    return out


def _drop_vars(node, names: set[str]):
    """`node` with `names` removed from every `vars:` mapping in it.

    A mapping left empty goes too, so a play that gained its first `vars:` for an assert
    compares equal to the play before it.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "vars" and isinstance(v, dict):
                v = {n: v2 for n, v2 in v.items() if n not in names}
                if v:
                    out[k] = v
            else:
                out[k] = _drop_vars(v, names)
        return out
    if isinstance(node, list):
        return [_drop_vars(item, names) for item in node]
    return node


def _is_play_file(path: str) -> bool:
    return path.startswith(_BROAD_PLAY_PREFIXES) and path.endswith((".yml", ".yaml"))


def _var_reader(name: str, head: str, repo: Path) -> str | None:
    """The first file at `head` that reads the play variable `name` outside an inert task.

    A play or task file reads it when the name survives in its parsed value once its inert
    tasks and every definition of `name` are gone. A hit anywhere else under `ansible/` (a
    role, a template, the inventory, a plugin) is a reader by itself.
    """
    r = git("grep", "-lwF", "-e", name, head, "--", "ansible/", cwd=repo, check=False)
    if r.returncode > 1:
        return f"`git grep {name}` failed"
    word = re.compile(rf"\b{re.escape(name)}\b")
    for line in r.stdout.splitlines():
        hit = line.split(":", 1)[1]
        if paths.is_prose(hit) or _is_test_only_path(hit):
            continue
        if not _is_play_file(hit):
            return hit
        text = show_at(head, hit, repo)
        try:
            doc = _drop_vars(_strip(hit, yaml_fast.safe_load(text or "")), {name})
        except yaml.YAMLError:
            return hit
        if word.search(json.dumps(doc, default=str)):
            return hit
    return None


def _play_reason(path: str, before: str, after: str, head: str, repo: Path):
    """Why a play file's change is not confined to inert tasks, or None when it is."""
    try:
        old = _strip(path, yaml_fast.safe_load(before))
        new = _strip(path, yaml_fast.safe_load(after))
    except yaml.YAMLError as exc:
        return f"{path} does not parse as YAML: {exc}"
    if old == new:
        return None
    old_defs, new_defs = _var_defs(old, Counter()), _var_defs(new, Counter())
    changed = {name for name, _ in (old_defs - new_defs) + (new_defs - old_defs)}
    if not changed or _drop_vars(old, changed) != _drop_vars(new, changed):
        return f"{path} changes a task that is not an assert, fail or debug"
    for name in sorted(changed):
        reader = _var_reader(name, head, repo)
        if reader:
            return f"{path} changes the variable {name}, which {reader} reads"
    return None


def _without_docstring(node):
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]


def _top_level(text: str) -> tuple[dict[str, tuple[str, set[str]]], list[str]]:
    """A module's top-level statements, with docstrings and comments left out.

    Returns:
        `name -> (dump, names it uses)` for each statement that binds a name, and the dumps
        of the statements that bind none.
    """
    tree = ast.parse(text)
    _without_docstring(tree)
    named: dict[str, tuple[str, set[str]]] = {}
    unnamed: list[str] = []
    for stmt in tree.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _without_docstring(node)
        uses = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [stmt.name]
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if len(names) != len(targets):
                names = []
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in stmt.names]
        else:
            names = []
        if not names:
            unnamed.append(ast.dump(stmt))
        for name in names:
            named[name] = (ast.dump(stmt), uses)
    return named, unnamed


def _registered(text: str) -> dict[str, str]:
    """`FilterModule.filters()`'s literal dict as filter name -> function name."""
    for cls in ast.parse(text).body:
        if isinstance(cls, ast.ClassDef) and cls.name == "FilterModule":
            for ret in (n for n in ast.walk(cls) if isinstance(n, ast.Return)):
                if isinstance(ret.value, ast.Dict):
                    return {
                        k.value: v.id
                        for k, v in zip(ret.value.keys, ret.value.values, strict=True)
                        if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and isinstance(v, ast.Name)
                    }
    return {}


def _closure(start: set[str], named: dict[str, tuple[str, set[str]]]) -> set[str]:
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name in seen or name not in named:
            continue
        seen.add(name)
        todo.extend(named[name][1])
    return seen


def order_only(path: str, before: str, after: str) -> bool:
    """Whether a filter plugin's change is confined to its `ORDER_ONLY_FILTERS` (#4781).

    True when every top-level name whose definition changed is reached by none of the
    plugin's other filters, at either end, and `FilterModule` itself is unchanged. False on
    any doubt: a plugin not in the table, a parse error, a changed statement that binds no
    name, or a registration this cannot read.
    """
    order = ORDER_ONLY_FILTERS.get(path)
    if not order:
        return False
    try:
        old, old_unnamed = _top_level(before)
        new, new_unnamed = _top_level(after)
        registered = _registered(before) | _registered(after)
    except SyntaxError:
        return False
    if old_unnamed != new_unnamed or not registered:
        return False
    changed = {
        n
        for n in old.keys() | new.keys()
        if old.get(n, ("",))[0] != new.get(n, ("",))[0]
    }
    others = {fn for name, fn in registered.items() if name not in order}
    reached = _closure(others, old) | _closure(others, new) | {"FilterModule"}
    return not changed & reached


def _parsed_same(path: str, before: str, after: str) -> bool:
    """Whether a YAML file Ansible parses has the same value at both ends."""
    at = role_of(path)
    parsed = _is_play_file(path) or (at is not None and at.subdir in _PARSED_ROLE_DIRS)
    if not (parsed and path.endswith((".yml", ".yaml"))):
        return False
    try:
        return yaml_fast.safe_load(before) == yaml_fast.safe_load(after)
    except yaml.YAMLError:
        return False


class Classifier:
    """The per-path rule, with the deploy-plane `Context` built once per HEAD."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._contexts: dict[str, broad.Context] = {}

    def _context(self, head: str) -> broad.Context:
        if head not in self._contexts:
            self._contexts[head] = broad.context_for(head, self.repo)
        return self._contexts[head]

    def reason(self, role: str, base: str, head: str) -> str | None:
        """Why the digest at `head` does not cover `base..head` for `role`, or None."""
        r = git("merge-base", "--is-ancestor", base, head, cwd=self.repo, check=False)
        if r.returncode != 0:
            return f"{base[:8]} is not an ancestor of {head[:8]}"
        diff = git("diff", "--name-only", base, head, cwd=self.repo, check=False)
        if diff.returncode != 0:
            return f"`git diff {base[:8]} {head[:8]}` failed"
        for path in diff.stdout.splitlines():
            why = self._path_reason(role, path, base, head)
            if why:
                return why
        return None

    def _path_reason(self, role: str, path: str, base: str, head: str) -> str | None:
        cs = services_from_changed_paths([path])
        own = role in cs.k8s
        plane = cs.broad_deploy and role in self._context(head).declared
        if not (own or plane):
            return None
        if paths.is_prose(path) or _is_test_only_path(path):
            return None
        before, after = show_at(base, path, self.repo), show_at(head, path, self.repo)
        if before is None or after is None:
            return f"{path} is added or deleted"
        if _parsed_same(path, before, after):
            return None
        if path.endswith(".j2") and narrow_templates.comment_only(before, after):
            return None
        if not plane:
            return f"{path} changes {role}'s own role"
        if filters.is_plugin(path) and order_only(path, before, after):
            return None
        if _is_play_file(path):
            return _play_reason(path, before, after, head, self.repo)
        try:
            tags = broad.broad_path_tags(path, base, self._context(head))
        except CannotNarrow as exc:
            return f"{path}: {exc}"
        return f"{path} reaches {role}" if role in tags else None


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "items", nargs="+", help="ROLE:BASE:HEAD, one per line to judge"
    )
    parser.add_argument(
        "--repo", default=".", help="the checkout to read (default: the cwd)"
    )
    args = parser.parse_args(argv)
    classifier = Classifier(Path(args.repo))
    out: dict[str, bool] = {}
    for item in args.items:
        role, _, rest = item.partition(":")
        base, _, head = rest.partition(":")
        try:
            why = (
                classifier.reason(role, base, head)
                if role and base and head
                else "not ROLE:BASE:HEAD"
            )
        except (CannotNarrow, OSError, ValueError, yaml.YAMLError) as exc:
            why = f"{type(exc).__name__}: {exc}"
        if why:
            print(f"digest_covers: {item}: kept, {why}", file=sys.stderr)
        out[item] = why is None
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
