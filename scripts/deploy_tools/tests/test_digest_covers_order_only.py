"""The #4842 rows of `digest_covers.py`'s case table: a toposort change judged by running it.

`test_digest_covers.py` holds the table and its throwaway tree. These rows extend it for the
order-only rule, which judges a `toposort.py` change by running the k8s sort chain at both
ends rather than by reading its syntax. A must-keep row writes an entry of `containers_list`
through a shape a syntax-reading check missed in review, or gives the rule cause for doubt. A
must-discharge row changes only the order of identical entries, whichever filter it is in.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only.py
"""

import pytest

import digest_covers
from lib.git_testing import git_out

from test_digest_covers import HOST_VARS, _commit, _dep_map_body, _edit, tree  # noqa: F401

# Must-keep rows: (id, a new top-level statement, what `build_k8s_dep_map` runs before its
# return).
ENTRY_WRITES = [
    # The four shapes #4842 names.
    (
        "lambda-writes",
        "_mark = lambda c: c.update(use_authelia=False)",
        "for c in containers_list:\n    _mark(c)",
    ),
    (
        "class-writes",
        "class _M:\n    def __init__(self, c):\n        c['use_authelia'] = False",
        "for c in containers_list:\n    _M(c)",
    ),
    (
        "dunder-writes",
        "",
        "for c in containers_list:\n    c.__setitem__('use_authelia', False)",
    ),
    (
        "operator-writes",
        "import operator",
        "for c in containers_list:\n    operator.setitem(c, 'use_authelia', False)",
    ),
    # The aliasing shapes #4844's review found after its fix round.
    (
        "aliased-bound-method-writes",
        "",
        "for c in containers_list:\n    f = c.update\n    f(use_authelia=False)",
    ),
    (
        "aliased-dunder-writes",
        "",
        "for c in containers_list:\n    f = c.__setitem__\n    f('use_authelia', False)",
    ),
    (
        "writes-through-a-local-holder",
        "",
        "for c in containers_list:\n"
        "    h = {}\n"
        "    h['e'] = c\n"
        "    h['e']['use_authelia'] = False",
    ),
    (
        "global-alias-writes",
        "import operator\n\n_W = operator.setitem",
        "for c in containers_list:\n    _W(c, 'use_authelia', False)",
    ),
    # Swapping two entries' names leaves the output a permutation of the same entries, so
    # only the check that the input list still equals its copy can see the write.
    (
        "swaps-names-in-place",
        "",
        "a, b = containers_list[0], containers_list[1]\n"
        "na, nb = a['name'], b['name']\n"
        "a.__setitem__('name', nb)\n"
        "b.__setitem__('name', na)",
    ),
    # Doubt keeps the line: an exception, or an import that fails.
    ("raises", "", "raise RuntimeError('boom')"),
    ("imports-a-missing-module", "import _no_such_module_4842", ""),
]


def _dep_map_writes(helper: str, body: str) -> str:
    """The table's `TOPOSORT` with `helper` above it and `build_k8s_dep_map` running `body`."""
    lines = "".join(f"    {line}\n" for line in body.split("\n") if line)
    return (f"{helper}\n\n\n" if helper else "") + _dep_map_body(lines)


ROWS = [
    # ── must discharge ──────────────────────────────────────────────────────────────────────
    pytest.param(
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                "    return list(containers_list)\n",
                "    return list(reversed(containers_list))\n",
            )
        ],
        None,
        id="toposort-containers-only-permutes-its-entries",
    ),
    pytest.param(
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                "    return [c for c in containers_list if _platform_of(c) == platform]\n",
                "    return list(filter(lambda c: _platform_of(c) == platform, containers_list))\n",
            )
        ],
        None,
        id="filter-by-platform-refactor-with-the-same-output",
    ),
    # ── must keep ───────────────────────────────────────────────────────────────────────────
    *(
        pytest.param(
            [{"ansible/filter_plugins/toposort.py": _dep_map_writes(helper, body)}],
            "every deploy runs",
            id=f"build-k8s-dep-map-{row}",
        )
        for row, helper, body in ENTRY_WRITES
    ),
]


@pytest.mark.parametrize("commits, kept_because", ROWS)
def test_the_order_only_rows(tree, commits, kept_because):  # noqa: F811
    """A must-keep row names the reason, so it cannot pass on an incidental one."""
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(tree, commits)
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    if kept_because is None:
        assert reason is None
    else:
        assert reason and kept_because in reason


def test_reordering_an_edge_set_is_order_only(tree):  # noqa: F811
    """Each role's edges in another order change the dependency map and no entry."""
    tree.write(
        "ansible/inventory/host_vars/daniel-box.yml",
        HOST_VARS + "    depends_on: [authelia, crowdsec]\n",
    )
    base = tree.commit("sonarr has two edges")
    head = _commit(
        tree,
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                "_edges(c) for c",
                "sorted(_edges(c), reverse=True) for c",
            )
        ],
    )
    assert digest_covers.Classifier(tree.root).reason("authelia", base, head) is None
