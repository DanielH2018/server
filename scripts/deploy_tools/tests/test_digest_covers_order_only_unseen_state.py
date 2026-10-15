"""The order-only rows for state the module-state comparison does not see (#5060, #5063).

`test_digest_covers_order_only.py` holds the order-only rows, and these would take it past
the 500-line test-module cap, so they extend it here. Each row's base end already imports what the head end uses. Loading
a module only the head loads already keeps the line, so that shared import keeps each row to
the one shape the issue names.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only_unseen_state.py
"""

import pytest

import digest_covers

from test_digest_covers import _commit, tree  # noqa: F401
from test_digest_covers import TOPOSORT

_DEP_MAP_RETURN = '    return {c["name"]: _edges(c) for c in containers_list}\n'


def _reason_for_head_call(tree, imports: str, call: str) -> str | None:  # noqa: F811
    """The rule's reason when the base imports `imports` and the head's dep map also runs `call`."""
    plugin = f"{imports}\n\n\n{TOPOSORT}"
    tree.write("ansible/filter_plugins/toposort.py", plugin)
    base = tree.commit("toposort imports what the head calls")
    lines = "".join(f"    {line}\n" for line in call.split("\n") if line)
    head = _commit(
        tree,
        [
            {
                "ansible/filter_plugins/toposort.py": plugin.replace(
                    _DEP_MAP_RETURN, lines + _DEP_MAP_RETURN
                )
            }
        ],
    )
    return digest_covers.Classifier(tree.root).reason("authelia", base, head)


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import sys", "sys.path.append('/order-only-5060')"),
        (
            "import warnings",
            "warnings.filters.insert(0, ('ignore', None, Warning, None, 0))",
        ),
        (
            "import copy",
            "class _T:\n    pass\ncopy._deepcopy_dispatch[_T] = lambda x, memo: x",
        ),
    ],
    ids=["sys-path-append", "warnings-filters-insert", "deepcopy-dispatch-item"],
)
def test_an_in_place_change_to_a_module_held_object_keeps_the_line(tree, imports, call):  # noqa: F811
    """An item added to a list or dict a module holds keeps the line (#5060).

    The attribute still names the same object, so comparing attribute identity sees nothing.
    In a deploy the plugin runs in the controller process, so the added item persists for
    every later task and role.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import os", "os.umask(0o077)"),
        ("import signal", "signal.signal(signal.SIGTERM, signal.SIG_IGN)"),
        ("import sys", "sys.setrecursionlimit(123456)"),
        ("import os", "os.nice(1)"),
        ("import os", "os.setsid()"),
    ],
    ids=["umask", "signal-handler", "recursion-limit", "nice", "setsid"],
)
def test_a_change_to_process_state_keeps_the_line(tree, imports, call):  # noqa: F811
    """Process state that no module attribute holds and no audit event reports keeps the line (#5060).

    The subprocess the runner uses discards the change when it exits. In a deploy the change
    persists in the controller process for every later task and role.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import _dbm", "_dbm.open({path}, 'c').close()"),
        (
            "import _posixshmem\nimport os",
            "os.close(_posixshmem.shm_open('/order-only-5063', os.O_CREAT | os.O_RDWR, 0o600))\n"
            "_posixshmem.shm_unlink('/order-only-5063')",
        ),
    ],
    ids=["dbm-open", "shm-open"],
)
def test_an_unaudited_file_creating_call_through_a_module_the_base_loads_keeps_the_line(
    tree,  # noqa: F811
    tmp_path,
    imports,
    call,
):
    """A file-creating call with no audit event keeps the line when the base loads its module (#5063).

    Both ends load the same modules, so the comparison of loaded modules reports nothing. With
    an absolute path the entry lands outside the runner's temp tree. In a deploy it lands
    wherever the path points.
    """
    path = repr(str(tmp_path / "escaped"))
    reason = _reason_for_head_call(tree, imports, call.format(path=path))
    assert reason and "every deploy runs" in reason
