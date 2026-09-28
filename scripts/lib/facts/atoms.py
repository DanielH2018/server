"""Resolve a citation against a checkout and hash the thing it names.

The hash is over the *thing the sentence is about*, at that granularity: a symbol's AST with
docstrings stripped (a reword must not invalidate a claim about a value), a YAML key's value
(a comment must not either), a marker line's text (moving it must not). ``None`` means the
atom does not resolve; a probe atom is never hashed here — its shape hash needs a live run.

A **path** atom is about location, not content, so its hash is over the path itself: the atom
resolves while the file or directory is there and reads ``None`` once it is gone, which is
what makes a delete or a rename a ``missing`` finding. The finer claim — a value, a key, a
decision, a test's body — has a form of its own, and those are the forms that content-hash.
Hashing a cited file's bytes instead made every edit anywhere in it a finding about a
sentence that only said where the file lives: 687 of the 1,253 commits in the 30 days to
2026-09-28 touched a cited file that way, and none of them corrected a documented claim.

A Python node is hashed through ``ast.unparse``, never ``ast.dump``. A dump names every AST
field, so a CPython release that adds one (``type_params`` in 3.12) moves every recorded
symbol and test hash at once, and the whole lock reads OUT after an interpreter upgrade that
changed nothing about the code. ``ast.unparse`` renders source, which is the thing the
citation is actually about. ``lock.check_lock`` carries the belt-and-braces half: a row
recorded under another minor version is reported rather than compared.
"""

import ast
import hashlib
import json
import re
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.yaml_fast import safe_load

from .citations import Citation

HASHED_FORMS = frozenset({"path", "symbol", "yaml", "test", "marker"})
_BACKREF = re.compile(r"^\s*#\s*fact:\s*(\S.*?)\s*$", re.MULTILINE)


class Ambiguous(Exception):
    """A marker prefix that matches more than one line — cite a longer prefix."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_docstrings(node: ast.AST) -> ast.AST:
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if (
            isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            n.body = body[1:] or [ast.Pass()]
    return node


def _binds(target: ast.expr, name: str) -> bool:
    """Whether an assignment target names ``name``, directly or inside an unpacking.

    ``A, B = 1, 2`` binds both; the atom for either is the whole statement, since that is
    the smallest node ``ast.unparse`` renders and the value one name gets is not separable
    from the tuple the other reads.
    """
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Starred):
        return _binds(target.value, name)
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_binds(e, name) for e in target.elts)
    return False


def _top_level(tree: ast.Module, name: str) -> ast.AST | None:
    for n in tree.body:
        if (
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name == name
        ):
            return n
        if isinstance(n, ast.Assign) and any(_binds(t, name) for t in n.targets):
            return n
        if (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            return n
    return None


def _test_node(tree: ast.Module, selector: str) -> ast.AST | None:
    """``test_x`` or ``Class::test_x``."""
    parts = selector.split("::")
    node: ast.AST | None = _top_level(tree, parts[0])
    if len(parts) == 2 and isinstance(node, ast.ClassDef):
        node = next(
            (
                n
                for n in node.body
                if isinstance(n, ast.FunctionDef) and n.name == parts[1]
            ),
            None,
        )
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _walk(value, key: str):
    """Follow a dotted selector; a digit part indexes a list or names an int key in a mapping.

    YAML reads an unquoted ``1:`` as the int 1, so a mapping keyed by port or by year is
    unreachable through the str the selector carries. The str key is tried first: a mapping
    holding both ``"1"`` and ``1`` is legal YAML, and the spelled form wins.
    """
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, dict) and part.isdigit() and int(part) in value:
            value = value[int(part)]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(key)
    return value


def _outside_repo(target: Path, repo: Path) -> bool:
    return not target.resolve().is_relative_to(repo.resolve())


def hash_atom(c: Citation, repo: Path) -> str | None:
    """The atom's hash now, or ``None`` when it does not resolve. Raises ``Ambiguous`` for a marker prefix matching twice."""
    if c.form not in HASHED_FORMS:
        return None
    target = repo / c.path
    if _outside_repo(target, repo):
        return None
    if c.form == "path":
        # DECIDED: a path atom hashes EXISTENCE, not content. See this module's docstring
        # for the rule and `.claude/rules/facts.md` for the measurement behind it.
        if c.path.endswith("/"):
            return _sha(c.path.encode()) if target.is_dir() else None
        return _sha(c.path.encode()) if target.is_file() else None
    if not target.is_file():
        return None
    if c.form == "symbol":
        node = _top_level(ast.parse(target.read_text(encoding="utf-8")), c.selector)
        return _sha(ast.unparse(_strip_docstrings(node)).encode()) if node else None
    if c.form == "yaml":
        try:
            value = _walk(safe_load(target.read_text(encoding="utf-8")), c.selector)
        except KeyError:
            return None
        return _sha(json.dumps(value, sort_keys=True, default=str).encode())
    if c.form == "test":
        node = _test_node(ast.parse(target.read_text(encoding="utf-8")), c.selector)
        return _sha(ast.unparse(_strip_docstrings(node)).encode()) if node else None
    if c.form == "marker":
        hits = [
            ln.strip()
            for ln in target.read_text(encoding="utf-8").splitlines()
            if f"DECIDED: {c.selector}" in ln
        ]
        if len(hits) > 1:
            raise Ambiguous(c.raw)
        return _sha(hits[0].encode()) if hits else None
    return None


def backrefs(c: Citation, repo: Path) -> frozenset[str]:
    """The ``# fact: <unit>`` lines inside a cited test function; empty for every other form."""
    if c.form != "test":
        return frozenset()
    target = repo / c.path
    if _outside_repo(target, repo) or not target.is_file():
        return frozenset()
    src = target.read_text(encoding="utf-8")
    node = _test_node(ast.parse(src), c.selector)
    if node is None:
        return frozenset()
    segment = ast.get_source_segment(src, node) or ""
    return frozenset(_BACKREF.findall(segment))
