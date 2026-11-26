"""The no-future-annotations census row and the pod-image code it exempts.

The row sits in its own module because `test_census_rows_python.py`, which imports it into
`ROWS`, is at the cap for a test module.
"""

import re

from _row_table import Census, Subject, tracked

FUTURE_ANNOTATIONS = re.compile(r"^from __future__ import annotations\b", re.MULTILINE)

# DECIDED: the pod-image `k8s/roles/*/files/*.py` are EXEMPT, and the exemption is not
# cosmetic. They run under whatever python their pod image ships, which can be older than 3.14.
# Below 3.14 there is no PEP 649, so annotations evaluate EAGERLY at def/class time and the
# import is load-bearing insurance against a forward reference — `def merge(self, other: Foo)
# -> Foo:` inside `class Foo` raises NameError without it. The repo suite cannot observe that:
# it runs on 3.14, where every such annotation is lazy either way. Host code is not exempt:
# every host runs Ubuntu 26.04, whose `/usr/bin/python3` is 3.14 (measured on daniel-server and
# daniel-pi on 2026-11-22), and each host package depends on `python3 (>= 3.14)`.
_POD_IMAGE_TREE = re.compile(r"^k8s/roles/[^/]+/files/")


def _uv_run_modules() -> list[str]:
    return [rel for rel in tracked("*.py") if not _POD_IMAGE_TREE.match(rel)]


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
        "Code that runs on a pod image's python is exempt: see the DECIDED marker."
    ),
    files=_uv_run_modules,
    offence=_future_offence,
    red=(Subject("a.py", '"""A module."""\n\nfrom __future__ import annotations\n'),),
    green=(
        Subject("a.py", '"""A module."""\n\nimport os\n'),
        Subject("b.py", "from __future__ import division\n"),
    ),
    min_matches=200,
    # One per context that reaches the uv interpreter, then one per context that reaches a host's
    # /usr/bin/python3.
    must_find=frozenset(
        {
            "scripts/renderer/template_filters/toposort.py",
            ".claude/hooks/block-footguns.py",
            "evals/trend.py",
            "tests/_helpers.py",
            "hosts/lib/host_lib.py",
            "hosts/components/claude-code/files/homelab_rootq.py",
        }
    ),
)
