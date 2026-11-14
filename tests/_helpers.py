"""Paths and readers shared by the guards in this directory.

Imported by name (`from _helpers import ...`) rather than through a conftest fixture: these are
plain functions over the filesystem, and the modules here already reach `_k8s_render` the same
way.
"""

import ast
import re
from pathlib import Path

from lib import yaml_fast
from lib.proc_testing import fake_bin, path_with, run

from lib.k8s_roles import ROLLOUT_TIMEOUT_DEFAULT

# `lib.repo_paths` owns the anchors. The `X as X` names are re-exports for the guards that
# import them from here; redefining one in a guard is what #3912 removed.
from lib.repo_paths import (
    ALL_VARS as ALL_VARS,
    HOST_VARS as HOST_VARS,
    INVENTORY as INVENTORY,
    K8S_ROLES as K8S_ROLES,
    REPO,
    SECRETS as SECRETS,
    SHARED_TPL as SHARED_TPL,
    TESTS as TESTS,
)


def load_yaml(path: Path):
    """The parsed document, or None for an empty file."""
    return yaml_fast.safe_load(path.read_text())


SECRET_REGISTRY = SECRETS / "secret_rotation.yml"

# Registry names too generic to match on: a whole-word hit on one would flag every script
# mentioning the word, and the guards that read this list would be turned off rather than
# obeyed. `domain` is in the registry at `tier: ignore` because it is a hostname, not a
# credential, and every script that builds a URL names it.
GENERIC_SECRET_NAMES = frozenset({"domain"})


def registry_secret_names() -> list[str]:
    """Every credential name in `secrets/secret_rotation.yml`, minus the generic ones.

    The registry is plaintext by design — names, dates and tiers, never values — so a guard
    built on it needs no SOPS key and runs on a CI runner. Read by the no_log census.
    """
    data = load_yaml(SECRET_REGISTRY) or {}
    return sorted(set(data.get("entries", {})) - GENERIC_SECRET_NAMES)


def load_defaults(role: Path) -> dict:
    """The parsed `defaults/main.yml` of a role directory, empty for a role with none.

    Takes the directory (`K8S_ROLES / "jellyfin"`, `K8S_ROLES / "nut"`) rather than a name:
    callers already hold the path, and a name would force a plane lookup that buys nothing.
    This is the RAW file. `lib.render_context` lays the defaults under the inventory and
    resolves them, which is what a render needs and what a guard on the literal text of a
    default must not get; `lib.estate.role_defaults(role)` is the docs generators' raw read.
    """
    return load_yaml(role / "defaults" / "main.yml") or {}


def image_tag(ref: str) -> str:
    """The tag of an image ref, with any `@sha256:` digest Renovate pinned beside it dropped.

    `authelia/authelia:4.39.28@sha256:bd97…` -> `4.39.28`. A bare `rsplit(":")` on that ref
    returns the digest hex, which is how the authelia and AutoKuma version guards failed their
    digest-pin PRs (#3557, #3564).
    """
    return ref.split("@", 1)[0].rsplit(":", 1)[-1]


# --- the k8s rollout budget, wherever a role spells it ---------------------------------------


# A role's rollout budget is `manifests_rollout_timeout | default(manifests_rollout_timeout_default)`,
# and three guards size themselves against it: the two rollback-budget tests, which read a role's
# call site, and the inline-gate budget census, which reads a `rollout status --timeout=`. All
# three read the literal text, so a role that moved its number into a variable — sonarr, which
# needs the SAME budget at its manifests call site and in its verify.yml gate — silently read as
# the shared default. Resolving the reference here, once, is what keeps that from being a green
# no-op.
_SECONDS = re.compile(r"^(\d+)s$")
_ROLE_VAR = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


# The shared default is READ from the renderer's constant rather than pinned here. A pinned
# copy would agree with the renderer only by coincidence, and the day the constant moved it
# would size every unoverridden role wrongly with every guard green.
def _manifests_rollout_default_s() -> int:
    """`ROLLOUT_TIMEOUT_DEFAULT`, the budget of a role with no timeout of its own, in seconds.

    RAISES when it is not spelled `<n>s`: a reader that fell back to a literal of its own would
    re-create the drift this function exists to remove.
    """
    seconds = _SECONDS.match(ROLLOUT_TIMEOUT_DEFAULT.strip())
    if not seconds:
        raise AssertionError(
            f"ROLLOUT_TIMEOUT_DEFAULT must read `<n>s`; read {ROLLOUT_TIMEOUT_DEFAULT!r}. Every "
            "guard that sizes a role's rollout budget reads it from there."
        )
    return int(seconds.group(1))


MANIFESTS_ROLLOUT_DEFAULT_S = _manifests_rollout_default_s()


def rollout_seconds(value, role: Path) -> int | None:
    """`"660s"` or `"{{ sonarr_k8s_rollout_timeout }}"` -> 660; None for anything else.

    `role` is the role DIRECTORY, because a `{{ var }}` is resolved against that role's own
    `defaults/main.yml`. Up to two references are chased, so the alias
    `sonarr_k8s_rollout_timeout: "{{ manifests_rollout_timeout }}"` resolves (#5464); anything
    else is unresolved, so a guard reading this gets None and says so.
    """
    text = str(value).strip().strip("\"'")
    for _ in range(3):
        if literal := _SECONDS.match(text):
            return int(literal.group(1))
        if not (reference := _ROLE_VAR.match(text)):
            return None
        text = str(load_defaults(role).get(reference.group(1), "")).strip()
    return None


_ROLLOUT_KEY = "manifests_rollout_timeout"


def manifests_rollout_timeout_s(role: Path) -> int:
    """The seconds a role declares for its rollout.

    The role's own `manifests_rollout_timeout` default (#5464), else
    `MANIFESTS_ROLLOUT_DEFAULT_S`. A budget this reader cannot resolve RAISES: the whole point
    is that an unreadable budget must not read as the default, which is how a 660s service
    would be sized as a 300s one.
    """
    declared = load_defaults(role).get(_ROLLOUT_KEY)
    if declared is None:
        return MANIFESTS_ROLLOUT_DEFAULT_S
    seconds = rollout_seconds(declared, role)
    if seconds is None:
        raise AssertionError(
            f"{role.name} passes manifests_rollout_timeout: {declared}, which this "
            "reader cannot resolve to a number of seconds. Spell it as `<n>s` or as a "
            "`{{ role_var }}` whose defaults/main.yml value is `<n>s`."
        )
    return seconds


_DOC_EXCLUDED_DIRS = (REPO / "docs" / "archive",)


def discover_docs() -> list[Path]:
    """Every operator doc in THIS checkout: repo CLAUDE.md files, `.claude/`, and `docs/`.

    DECIDED: `git ls-files`, not `rglob`. An rglob walks whatever is on disk, and this repo
    grows other checkouts during ordinary work — `.claude/worktrees/<name>/` is a full working
    tree per live session. Those hold OLDER copies of these same docs, so a guard that walks
    them judges this commit against other sessions' history and fails on paths that moved
    perfectly legitimately.

    The `docs-refresh` cron hits this: its commit runs the prek hooks, and a guard that counts
    citations inside sibling worktrees makes the script take its failure path and stop
    publishing. CI never shows it, because a CI runner has no worktrees on disk.

    `git ls-files` answers with what this commit actually contains, which is the only thing
    these guards have any business asserting about.
    """
    listed = run(["git", "ls-files", "-z", "--", "*.md"], cwd=REPO, check=True).stdout
    docs = []
    for rel in listed.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if path.name != "CLAUDE.md" and rel.split("/")[0] not in (".claude", "docs"):
            continue
        if any(excluded in path.parents for excluded in _DOC_EXCLUDED_DIRS):
            continue
        docs.append(path)
    return sorted(set(docs))


# --- Python modules under a role's files/ ----------------------------------------------------
#
# A role's runtime modules can sit at any depth under files/ — `files/bridge/common.py` is the
# module `bridge.common` — and a guard that reads them must not assume one level. A one-level
# glob returns an EMPTY set the moment the modules move down a directory, and every `all(...)`
# over it passes; repo-root CLAUDE.md's "a check that finds its own subject by pattern" rule
# records five guards that broke exactly that way. These readers identify a module by its
# dotted path under the root, so a flat `bridge/config.py` and a nested `bridge/config.py`
# are each one id, and resolve an import to that id in every spelling Python allows.


def is_test_file(path: Path) -> bool:
    """Whether `path` is test code, by basename or by living under a `tests/` directory.

    Pass a path relative to the tree being judged (a repo-relative path, or one relative to
    the `root` a caller is walking). The directory clause reads every component, so an
    absolute path could match a `tests` component that is part of the checkout's own location
    rather than of the repo layout.
    """
    return (
        path.name.startswith("test_")
        or path.name == "conftest.py"
        or "tests" in path.parts[:-1]
    )


def module_id(path: Path, root: Path) -> str:
    """`root/bridge/common.py` -> "bridge.common"; `root/check.py` -> "check"."""
    return path.relative_to(root).with_suffix("").as_posix().replace("/", ".")


def python_modules(root: Path) -> dict[str, Path]:
    """Dotted id -> file for every runtime module under `root`, at any depth.

    Test files and `__pycache__` are excluded; a test suite belongs in the sibling `tests/`
    directory, and the name filter is the belt-and-braces check against one landing here.
    """
    return {
        module_id(p, root): p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts and not is_test_file(p.relative_to(root))
    }


def imported_module_ids(tree: ast.AST, ids) -> set[str]:
    """The ids in `ids` that `tree` imports, in any form.

    `import a`, `import p.q [as r]`, `from p.q import X` and `from p import q` all count as an
    import of the module they name; a `from p.q import X` counts for `p.q`, not for `X`.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name in ids}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module in ids:
                found.add(node.module)
            found |= {
                f"{node.module}.{alias.name}"
                for alias in node.names
                if f"{node.module}.{alias.name}" in ids
            }
    return found


def import_bindings(tree: ast.AST, ids) -> dict[str, str]:
    """Local name -> dotted prefix, for every import in `tree` that can reach one of `ids`.

    `import a` binds `a`; `import a as b` binds `b` to `a`; `import p.q` binds the package
    `p`, so `p.q.X` resolves through it; `import p.q as r` binds `r` to `p.q`; `from p import
    q [as r]` binds `q` (or `r`) to `p.q` when that is a module in `ids`. A `from p.q import X`
    binds a plain name, not a module, and is not recorded.
    """
    bound = {}
    packages = {i.rsplit(".", 1)[0] for i in ids if "." in i}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    if alias.name in ids:
                        bound[alias.asname] = alias.name
                    continue
                head = alias.name.split(".")[0]
                if (
                    head in ids
                    or head in packages
                    or any(p.startswith(head + ".") for p in packages)
                ):
                    bound[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted in ids:
                    bound[alias.asname or alias.name] = dotted
    return bound


def module_of(node: ast.AST, bound: dict[str, str], ids) -> str | None:
    """The module id a `Name` or `a.b.c` attribute chain refers to, through `bound`, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bound:
        return None
    dotted = ".".join([bound[node.id], *reversed(parts)])
    return dotted if dotted in ids else None


# The stub records one line per call so a test can assert it intercepted something. Without
# that record the fixture would be indistinguishable from one that silently stopped being on
# PATH: a stub no longer on PATH fails OPEN, the run stays green, and the real `logger` takes
# every call again.
_LOGGER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOGGER_CALLS"
"""


def stub_logger_on_path(tmp_path_factory, monkeypatch) -> Path:
    """Put a fake `logger` first on PATH so a test writes nothing to the host's syslog.

    Returns the file the stub appends to, one line per call, holding `logger`'s arguments
    verbatim (`-t <tag> <message>`).

    A test that runs a host script as a real subprocess inherits this when it builds its env
    from `os.environ`: its own stub directory, prepended later, still wins for `k3s` or `gh`,
    and this wins for `logger` over `/usr/bin/logger`. A script that starts calling `logger`
    by absolute path escapes it, which is why each directory using this also keeps one test
    asserting the stub RECORDED a call.

    Why it matters: the tags under test are shipped to Loki, so a fixture verdict written to
    the real syslog sits on a dashboard beside real ones.
    """
    stub_dir = fake_bin(tmp_path_factory.mktemp("logger-stub"), logger=_LOGGER_STUB)

    calls = stub_dir / "logger-calls"
    calls.touch()
    monkeypatch.setenv("TEST_LOGGER_CALLS", str(calls))
    monkeypatch.setenv("PATH", path_with(stub_dir))
    return calls
