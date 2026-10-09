#!/usr/bin/env python3
"""What a filter plugin defines, and which grep hits for its filter names are callers (#3843).

THE PROBLEM. `ansible/filter_plugins/` is a play-level prefix, so `narrow_broad` refused every
change under it as "read by every deploy" and the tick ran the whole `ansible/deploy.yml`. That
is right for a filter the play calls (`toposort.py`'s, from `deploy.yml`), and wrong for one
only role templates call: `py_table.py` reaches monitor-bridge and uptime-kuma and nothing else.

WHAT THIS KNOWS. The filter names a plugin registers, read from the literal dict its
`FilterModule.filters()` returns, at both ends of the range so a renamed filter keeps its old
callers. And which grep hits for one of those names are callers. `narrow_broad._filter_tags`
maps the callers to tags with the same `_sort_hits` the macro and variable rules use, so a
caller under the play's own trees still refuses there.

ANY DOUBT IS A REFUSAL: a `filters()` that is not a literal dict of string keys, a deleted
plugin, a plugin a sibling plugin imports, and a mention in an inventory value all raise
`CannotNarrow`. Lives beside `narrow_broad` because that module sits at its 600-line cap; it
imports nothing from it, so the two cannot cycle.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from deploy_tools import narrow_templates
from lib.git import git
from lib.narrow_git import CannotNarrow, show_at

PLUGINS = "ansible/filter_plugins/"
_INVENTORY = "ansible/inventory/"
_YAML_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)


def is_plugin(path: str) -> bool:
    """Whether `path` is a filter plugin module, the one shape this rule reads."""
    rest = path.removeprefix(PLUGINS)
    return rest != path and "/" not in rest and rest.endswith(".py")


def filter_names(text: str, path: str) -> set[str]:
    """The filter names `text`'s `FilterModule.filters()` returns, or a refusal.

    Parsed, never imported: the plugins import `ansible` and `yaml`, and an import runs code
    the deployer has not vetted. Only `return {"name": fn, ...}` is read, since a dict built
    any other way names filters no static read can list.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise CannotNarrow(f"{path} does not parse: {exc}") from exc
    for cls in tree.body:
        if not (isinstance(cls, ast.ClassDef) and cls.name == "FilterModule"):
            continue
        for fn in cls.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "filters"):
                continue
            returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
            if len(returns) == 1 and isinstance(returns[0].value, ast.Dict):
                # A `**other` entry has a None key, which this skips and so refuses.
                names = {
                    k.value
                    for k in returns[0].value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if names and len(names) == len(returns[0].value.keys):
                    return names
    raise CannotNarrow(f"{path} returns no literal dict of filter names from filters()")


def plugin_names(path: str, old_ref: str, new_ref: str, cwd: Path) -> set[str]:
    """Every filter name `path` registers at either ref, after the two plugin-level refusals.

    A deleted plugin refuses: a caller left behind fails to render, and that is a full run's
    finding, not a narrowing's. A plugin another plugin imports refuses too, since the
    importer's filters change with it and are not in this file's `filters()`.
    """
    after = show_at(new_ref, path, cwd)
    if after is None:
        raise CannotNarrow(f"{path} was deleted, so nothing can be read from it")
    before = show_at(old_ref, path, cwd)
    names = filter_names(after, path)
    if before is not None:
        names |= filter_names(before, path)
    stem = re.escape(path.removeprefix(PLUGINS).removesuffix(".py"))
    r = git(
        "grep",
        "-l",
        "-E",
        "-e",
        rf"^\s*(from|import)\s+{stem}\b",
        new_ref,
        "--",
        PLUGINS,
        cwd=cwd,
        check=False,
    )
    if r.returncode > 1:
        raise CannotNarrow(
            f"`git grep` for importers of {path} failed: {r.stderr.strip()}"
        )
    importers = {line.split(":", 1)[1] for line in r.stdout.splitlines() if line} - {
        path
    }
    if importers:
        raise CannotNarrow(f"{path} is imported by {', '.join(sorted(importers))}")
    return names


def callers(hits: Iterable[str], name: str, ref: str, cwd: Path) -> list[str]:
    """`hits` less every path that names the filter `name` without being able to call it.

    Dropped: a `.md`, a `.py` under `PLUGINS` (Python registers a filter, it never pipes into
    one), a `.j2` naming it only inside a Jinja comment, and an inventory file naming it only
    on whole-line comments. Any other mention counts, `| name`, `map('name')` and prose in a
    role's own YAML alike; the over-count costs a redeploy, a miss leaves a service stale.

    An inventory value that does call the filter refuses: whatever reads that value is the
    caller, and the key diff cannot see it. The whole-line comment skip is the one
    `narrow_broad._defines_only` makes, with the block-scalar caveat its docstring names.
    """
    kept = []
    word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    for path in narrow_templates.real_mentions(hits, name, ref, cwd):
        if path.endswith(".md") or (path.startswith(PLUGINS) and path.endswith(".py")):
            continue
        if path.startswith(_INVENTORY):
            if path.split("/")[-1].startswith("_"):
                continue
            if word.search(_YAML_COMMENT_LINE.sub("", show_at(ref, path, cwd) or "")):
                raise CannotNarrow(f"the filter {name} is called by a value in {path}")
            continue
        kept.append(path)
    return kept
