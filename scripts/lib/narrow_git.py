"""The reads the two narrowing derivations share: a refusal, a `git show`, and a key diff.

`scripts/deploy_tools/narrow_broad.py` asks which service tags a deploy-plane change reaches.
`scripts/deploy_tools/narrow_setup.py` asks which `--tags` value a setup-role change needs.
Different questions over different trees, and both answer them by reading a file at a git ref,
parsing it as a YAML mapping, and refusing whenever a rule cannot say. Each carried its own
copy of those three primitives until #2419, and the copies had drifted: two `CannotNarrow`
classes that no `except` could catch together, two `git show` wrappers, and two inline key
diffs, only one of which survived a recursive YAML alias.

WHY ONE `CannotNarrow` AND NOT TWO. The two modules already meet — `shared_role_reach` imports
the setup half and `probe_lib/releases` catches the broad half — so a raise crossing that
boundary had to match the class the catch site happened to import. One class makes that
question disappear.

Import it as `lib.narrow_git`, never bare. `lib` is reached under exactly that name everywhere
in this repo, and a module imported under two names is two classes: an `except CannotNarrow`
written against one of them does not catch the other.

Run: uv run pytest scripts/lib/tests/test_narrow_git.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import yaml

from lib import yaml_fast
from lib.git import git


class CannotNarrow(Exception):
    """This change reaches something no narrow tag list covers.

    Both callers act on it by widening: the deploy plane runs the whole `ansible/deploy.yml`
    play, and the setup plane prints the whole-role tag. Neither is wrong, only slow — so any
    doubt raises this rather than guessing a narrower answer.
    """


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


def mapping_at(text: str, label: str) -> dict:
    """`text` parsed as a YAML mapping, or a refusal naming `label`.

    An empty file is an empty mapping: a vars file emptied by a range defines no key, which is
    a real answer rather than a doubt.
    """
    try:
        loaded = yaml_fast.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise CannotNarrow(f"{label} does not parse: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CannotNarrow(f"{label} is not a mapping of keys")
    return loaded


def changed_mapping_keys(before: dict, after: dict, label: str) -> set[str]:
    """The top-level keys whose value differs between two parsed mappings.

    A key removed counts as changed: the tasks and templates reading it change behaviour.
    """
    try:
        return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    except RecursionError as exc:
        # A recursive alias (`k: &l [1, *l]`) loads as a value containing itself, and `!=`
        # recurses into it until the stack runs out.
        raise CannotNarrow(f"{label} holds a recursive alias") from exc
