"""The reads the two narrowing derivations share: a refusal, a `git show`, and a key diff.

`scripts/deploy_tools/narrow_lib/broad.py` asks which service tags a deploy-plane change reaches.
`scripts/deploy_tools/narrow_setup.py` asks which `--tags` value a setup-role change needs.
Different questions over different trees, and both answer them by reading a file at a git ref,
parsing it as a YAML mapping, and refusing whenever a rule cannot say.

WHY ONE `CannotNarrow` AND NOT TWO. A raise crossing from one module to a caller of the other
would have to match the class the catch site happened to import. One class makes that question
disappear.

Import it as `lib.narrow_git`, never bare. `lib` is reached under exactly that name everywhere
in this repo, and a module imported under two names is two classes: an `except CannotNarrow`
written against one of them does not catch the other.

Run: uv run pytest scripts/lib/tests/test_narrow_git.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import contextlib
from collections.abc import Callable, Generator

from lib import yaml_fast
from lib.git import git


class CannotNarrow(Exception):
    """This change reaches something no narrow tag list covers.

    Both callers act on it by widening: the deploy plane's census marks every service it may
    reach stale, and the setup plane prints the whole-role tag. Neither is
    wrong, only coarse, so any doubt raises this rather than guessing a narrower answer.

    Attributes:
        reached: the tags the derivation did reach, when the refusal is about their number
            rather than about what it could not read. Empty for every other refusal.
    """

    def __init__(self, message: str, reached: set[str] | None = None) -> None:
        super().__init__(message)
        self.reached = frozenset(reached or ())


def show_at(ref: str, path: str, repo) -> str | None:
    """The file's text at `ref`, or None when the ref does not carry it.

    A file that is not UTF-8 text refuses rather than raising: the scans above this read text,
    and a traceback in the deployer's journal is a worse way to say "cannot narrow" than a
    refusal the caller already knows how to widen from.

    Args:
      ref: the commit to read at.
      path: the repo-relative path.
      repo: the checkout to read, as a str or a Path. `cwd` alone decides which repository is
        read — `lib.git.git` strips every `GIT_*` variable.
    """
    try:
        r = git("show", f"{ref}:{path}", cwd=repo, check=False)
    except UnicodeDecodeError as exc:
        raise CannotNarrow(f"{path} is not text at {ref}") from exc
    return r.stdout if r.returncode == 0 else None


@contextlib.contextmanager
def refuse_deep_walk(label: str) -> Generator[None]:
    """Refuse when a recursive walk over a parsed value runs out of stack.

    A recursive alias (`k: &l [1, *l]`) loads as a value containing itself, and very deep
    nesting exhausts the stack the same way.

    Raises:
        CannotNarrow: the body raised `RecursionError`.
    """
    try:
        yield
    except RecursionError as exc:
        raise CannotNarrow(
            f"{label} holds a recursive alias or nests too deeply"
        ) from exc


def walk_yaml[T](text: str, label: str, walk: Callable[[object], T]) -> T:
    """`walk` over `text`'s parsed value, or a refusal naming `label`.

    Raises:
        CannotNarrow: the text does not parse, or `walk` runs out of stack.
    """
    with refuse_deep_walk(label):
        try:
            doc = yaml_fast.parse_yaml(text)
        except yaml_fast.UnparsableYaml as exc:
            raise CannotNarrow(f"{label} does not parse: {exc}") from exc
        return walk(doc)


def mapping_at(text: str, label: str) -> dict:
    """`text` parsed as a YAML mapping, or a refusal naming `label`.

    An empty file is an empty mapping: a vars file emptied by a range defines no key, which is
    a real answer rather than a doubt.
    """
    loaded = walk_yaml(text, label, lambda doc: doc) or {}
    if not isinstance(loaded, dict):
        raise CannotNarrow(f"{label} is not a mapping of keys")
    return loaded


def same_value(a, b) -> bool:
    """Whether two parsed YAML values are the same, type and mapping key order included.

    `repr` as well as `==`, because Python reads `1`, `1.0` and `true` as equal and a rendered
    template does not.

    Raises:
        RecursionError: both values hold a recursive alias, which `==` cannot walk.
    """
    return a == b and repr(a) == repr(b)


def same_parsed_yaml(before: str, after: str) -> bool:
    """Whether two YAML texts load to the same value, so only comments or layout moved.

    A key reordered reads as a change, and text that does not load is never the same.
    """
    try:
        return same_value(yaml_fast.parse_yaml(before), yaml_fast.parse_yaml(after))
    except yaml_fast.UnparsableYaml, RecursionError:
        return False


def changed_mapping_keys(before: dict, after: dict, label: str) -> set[str]:
    """The top-level keys whose value differs between two parsed mappings.

    A key removed counts as changed: the tasks and templates reading it change behaviour.
    """
    with refuse_deep_walk(label):
        return {
            k
            for k in set(before) | set(after)
            if not same_value(before.get(k), after.get(k))
        }
