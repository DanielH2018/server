"""The no-future-annotations census row and the host code it exempts.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import re
import tomllib
from fnmatch import fnmatch
from functools import cache

from _helpers import REPO
from _row_table import Census, Subject, tracked

FUTURE_ANNOTATIONS = re.compile(r"^from __future__ import annotations\b", re.MULTILINE)

# DECIDED: host-shipped `k8s/roles/*/files/*.py`, `hosts/lib/` and the component programs that
# start under `/usr/bin/python3` are EXEMPT, and the exemption is not cosmetic.
# `hosts/lib/host_lib.py` runs under the hosts' own interpreter (`python3 -V` on daniel-server
# and daniel-pi reports 3.12.3, measured 2026-09-05), for instance through the repository
# signer, and the k8s files run under whatever python their pod image ships. Ruff's
# `per-file-target-version` in pyproject.toml already names each component file that runs on
# 3.12, so the census reads its py312 entries rather than keep a second list. Every other
# component file, and the two `hosts/lib` entry points in `_UV_PINNED_HOST_LIB`, starts through
# `uv run --python {{ host_python_version }}` (3.15.2), where the import is dead.
# `scripts/lib/longhorn_lib/` is not exempt: its reader runs under the pinned 3.15.2, and three
# of its modules use the parenthesis-free `except A, B:` that 3.12 rejects.
# Below 3.14 there is no PEP 649, so annotations evaluate EAGERLY at def/class time and the import is load-bearing insurance against a
# forward reference — `def merge(self, other: Foo) -> Foo:` inside `class Foo` raises
# NameError without it. The repo suite cannot observe that: it runs on 3.14, where every such
# annotation is lazy either way. So the exemption is what keeps a green suite from
# authorising a cron-time NameError on a host.
_HOST_INTERPRETER_TREES = re.compile(r"^(k8s/roles/[^/]+/files/|hosts/lib/[^/]+\.py$)")
_UV_PINNED_HOST_LIB = frozenset(
    {"hosts/lib/checkout_sync.py", "hosts/lib/host_package_builder.py"}
)


@cache
def _py312_component_globs() -> tuple[str, ...]:
    ruff = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["ruff"]
    return tuple(
        glob
        for glob, target in ruff["per-file-target-version"].items()
        if target == "py312" and glob.startswith("hosts/components/")
    )


def host_shipped(rel: str) -> bool:
    """Whether `rel` runs under a host interpreter older than 3.14, so may keep the import."""
    if rel in _UV_PINNED_HOST_LIB:
        return False
    return bool(_HOST_INTERPRETER_TREES.match(rel)) or any(
        fnmatch(rel, glob) for glob in _py312_component_globs()
    )


def _uv_run_modules() -> list[str]:
    return [rel for rel in tracked("*.py") if not host_shipped(rel)]


def _future_offence(subject: Subject) -> list[str]:
    return (
        ["imports annotations from __future__"]
        if FUTURE_ANNOTATIONS.search(subject.text)
        else []
    )


NO_FUTURE_ANNOTATIONS = Census(
    name="no-future-annotations",
    reason=(
        "On the repo's uv interpreter (3.14) PEP 649 already defers annotation evaluation, "
        "so `from __future__ import annotations` is dead weight, and half a directory "
        "carrying it is how the convention drifts (docs/python-code-organization.md). "
        "Host code that runs on 3.12 or a pod image's python is exempt: see the DECIDED "
        "marker."
    ),
    files=_uv_run_modules,
    offence=_future_offence,
    red=(Subject("a.py", '"""A module."""\n\nfrom __future__ import annotations\n'),),
    green=(
        Subject("a.py", '"""A module."""\n\nimport os\n'),
        Subject("b.py", "from __future__ import division\n"),
    ),
    min_matches=200,
    # One per context that reaches the uv interpreter: a template filter, a Claude hook, an
    # eval script and the suite's shared helper.
    must_find=frozenset(
        {
            "scripts/renderer/template_filters/toposort.py",
            ".claude/hooks/block-footguns.py",
            "evals/trend.py",
            "tests/_helpers.py",
        }
    ),
)
