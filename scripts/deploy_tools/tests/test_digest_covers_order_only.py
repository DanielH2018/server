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

from test_digest_covers import _commit, _dep_map_body, _edit, tree  # noqa: F401
from test_digest_covers import HOST_VARS
from test_digest_covers import TOPOSORT

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
    """Each role's edges in another order change the dependency map and no entry.

    The edges are sorted in place on a shallow copy of the entry's list. A syntax reading
    cannot tell that copy from the entry's own list, and #4841's write check refused it.
    """
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
                '    return {c["name"]: _edges(c) for c in containers_list}\n',
                "    dep_map = {}\n"
                "    for c in containers_list:\n"
                "        deps = list(_edges(c))\n"
                "        deps.sort(reverse=True)\n"
                '        dep_map[c["name"]] = deps\n'
                "    return dep_map\n",
            )
        ],
    )
    assert digest_covers.Classifier(tree.root).reason("authelia", base, head) is None


@pytest.mark.parametrize(
    "body",
    [
        "for c in containers_list:\n    for f in [c.update]:\n        f(use_authelia=False)",
        "for c in containers_list:\n    [f(use_authelia=False) for f in [c.update]]",
        "for c in containers_list:\n    (f := c.__setitem__)\n    f('use_authelia', False)",
        "for c in containers_list:\n    h = []\n    h.append(c)\n    h[0]['use_authelia'] = False",
        "sorted(containers_list, key=dict.clear)",
    ],
    ids=[
        "loop-alias",
        "comprehension-alias",
        "walrus-alias",
        "list-holder",
        "sorted-key",
    ],
)
def test_4844s_other_aliasing_shapes_keep_the_line(tree, body):  # noqa: F811
    """The aliasing rows #4844 added to its static check, which the running check sees as writes."""
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree, [{"ansible/filter_plugins/toposort.py": _dep_map_writes("", body)}]
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "body",
    [
        "for c in containers_list:\n    open(c['name'], 'w').write('x')",
        "for c in containers_list:\n    open(c['name'], 'a').write('x')",
    ],
    ids=["open-write", "open-append"],
)
def test_a_dep_map_that_writes_a_file_keeps_the_line(tree, body):  # noqa: F811
    """A file write leaves every entry and the input as they were, and still keeps the line (#4846).

    In a deploy the file lands in the controller's cwd, where a later role could read it, so
    the returned entries alone cannot prove the change order-only.
    """
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree, [{"ansible/filter_plugins/toposort.py": _dep_map_writes("", body)}]
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "helper, body",
    [
        ("", "try:\n    open('marker', 'w').write('x')\nexcept Exception:\n    pass"),
        ("import os", "os.mkdir('marker')"),
        ("import os", "os.putenv('MARKER', '1')"),
        ("import subprocess", "subprocess.run(['true'])"),
        ("open('marker', 'w').write('x')", ""),
        ("import os", "os.mkfifo('marker')"),
        ("import os\nimport stat", "os.mknod('marker', stat.S_IFREG | 0o600)"),
        # Rebinding what the hook might look up at call time, before the write.
        (
            "import os\n\nos._exit = lambda code: None",
            "open('marker', 'w').write('x')",
        ),
        (
            "import __main__\n\n__main__._WRITE_FLAGS = 0",
            "open('marker', 'w').write('x')",
        ),
        # Walking the heap to reach the hook's closure.
        ("import gc", "gc.get_objects()"),
    ],
    ids=[
        "swallowed-write",
        "mkdir",
        "putenv",
        "subprocess",
        "module-level-write",
        "mkfifo",
        "mknod",
        "rebinds-os-exit",
        "rebinds-runner-globals",
        "gc-walk",
    ],
)
def test_a_dep_map_with_another_side_effect_keeps_the_line(tree, helper, body):  # noqa: F811
    """The runner's audit hook ends the run on each of these, even inside an `except` (#4846)."""
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree, [{"ansible/filter_plugins/toposort.py": _dep_map_writes(helper, body)}]
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "body",
    [
        "copy.Error = None",
        "copy.SOME_SETTING = 'x'",
    ],
    ids=["rebinds-an-attribute", "adds-an-attribute"],
)
def test_a_dep_map_that_writes_another_modules_state_keeps_the_line(tree, body):  # noqa: F811
    """A write into another imported module's attributes keeps the line (#4995).

    No audit event reports it, and the entries and the input are unchanged. In a deploy,
    Ansible imports the plugin into the controller process, so the change persists for every
    later task. `copy` is a module the runner has imported already and does not read after the
    chain runs, so the row reaches the rule rather than failing the run.
    """
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree,
        [{"ansible/filter_plugins/toposort.py": _dep_map_writes("import copy", body)}],
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "call",
    ["os.mknod({path}, stat.S_IFREG | 0o600)", "os.mkfifo({path})"],
    ids=["mknod", "mkfifo"],
)
def test_a_dep_map_that_creates_a_file_outside_the_temp_tree_keeps_the_line(
    tree,  # noqa: F811
    tmp_path,
    call,
):
    """A call that raises no audit event and creates a file at an absolute path keeps the line (#4999).

    The path lies outside the runner's `order-only-` temp tree, so the check for paths created
    under that tree does not see it either. In a deploy the entry lands wherever the path
    points, where a later role could read it.
    """
    path = repr(str(tmp_path / "escaped"))
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_writes(
                    "import os\nimport stat",
                    f"try:\n    {call.format(path=path)}\nexcept FileExistsError:\n    pass",
                )
            }
        ],
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "helper, body",
    [
        ("import copy\n\ncopy.SOME_SETTING = 'x'", ""),
        ("", "import graphlib"),
    ],
    ids=["module-level-write", "loads-a-module"],
)
def test_module_state_the_base_does_not_leave_keeps_the_line(tree, helper, body):  # noqa: F811
    """A write at import time, or a module only the head loads, keeps the line (#4995).

    The runner compares the module state each end leaves behind from the plugin's import on, so
    module-level code counts as much as the chain. A module loaded during the run has no
    baseline to diff its attributes against, so loading one the base does not load is itself a
    difference.
    """
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree, [{"ansible/filter_plugins/toposort.py": _dep_map_writes(helper, body)}]
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


def test_module_state_both_ends_leave_is_order_only(tree):  # noqa: F811
    """A write the base already makes is compared, not refused (#4995).

    A write both ends make is not part of the change under review. Such a write would also
    keep every later line, including order-only ones, if the rule refused it.
    """
    writes = "import copy\n\ncopy.SOME_SETTING = 'x'\n\n\n" + TOPOSORT
    tree.write("ansible/filter_plugins/toposort.py", writes)
    base = tree.commit("toposort writes copy.SOME_SETTING")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": writes.replace(
                    "    return list(containers_list)\n",
                    "    return list(reversed(containers_list))\n",
                )
            }
        ],
    )
    assert digest_covers.Classifier(tree.root).reason("authelia", base, head) is None


def test_a_dep_map_that_rewrites_the_mknod_stubs_cell_keeps_the_line(tree):  # noqa: F811
    """The `mknod` stub the runner installs holds no cell the hook reads (#4999).

    A plugin can reach a stub through `os.mknod.__closure__`, and rewriting a cell raises no
    audit event. A stub sharing the hook's exit cell would let it turn the hook's every exit
    into a no-op before an environment write, which no path scan sees.
    """
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_writes(
                    "import os\n\nos.mknod.__closure__[0].cell_contents = lambda code: None",
                    "os.putenv('MARKER', '1')",
                )
            }
        ],
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


def test_a_write_into_a_module_the_plugin_loads_at_both_ends_keeps_the_line(tree):  # noqa: F811
    """A write into a module the runner has not loaded before the plugin keeps the line (#4995).

    Both ends import `socket`, so it loads after the runner's copy of module state, as all of
    Ansible does for the real plugin. In a deploy the controller has it loaded already and the
    write persists, so the runner's second run imports it before the copy.
    """
    plugin = "import socket\n\n\n" + TOPOSORT
    tree.write("ansible/filter_plugins/toposort.py", plugin)
    base = tree.commit("toposort imports socket")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": plugin.replace(
                    '    return {c["name"]: _edges(c) for c in containers_list}\n',
                    "    socket.SOME_SETTING = 'x'\n"
                    '    return {c["name"]: _edges(c) for c in containers_list}\n',
                )
            }
        ],
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "helper, body",
    [
        ("import _dbm", "_dbm.open({path}, 'c').close()"),
        (
            "import _posixshmem\nimport os",
            "os.close(_posixshmem.shm_open('/order-only-4999', os.O_CREAT | os.O_RDWR, 0o600))\n"
            "_posixshmem.shm_unlink('/order-only-4999')",
        ),
    ],
    ids=["dbm-open", "shm-open"],
)
def test_another_unaudited_file_creating_call_keeps_the_line(
    tree,  # noqa: F811
    tmp_path,
    helper,
    body,
):
    """A file-creating call outside `os` that raises no audit event keeps the line (#4999).

    The runner replaces these calls with one that ends the run (#5063). The call's module is
    also one the base end does not load, which the comparison of loaded modules reports.
    """
    path = repr(str(tmp_path / "escaped"))
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_writes(
                    helper, body.format(path=path)
                )
            }
        ],
    )
    reason = digest_covers.Classifier(tree.root).reason("authelia", base, head)
    assert reason and "every deploy runs" in reason


def test_rewriting_the_mknod_stubs_cell_alone_is_order_only(tree):  # noqa: F811
    """The control for the stub-cell row: the rewrite runs, and by itself changes nothing seen.

    So the stub-cell row is kept because the hook still ends the run on `os.putenv`, not
    because the rewrite failed, as it does where `os.mknod` is the builtin with no closure.
    """
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_writes(
                    "import os\n\nos.mknod.__closure__[0].cell_contents = lambda code: None",
                    "",
                )
            }
        ],
    )
    assert digest_covers.Classifier(tree.root).reason("authelia", base, head) is None
