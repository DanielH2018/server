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


def _reason_for_head_call(
    tree,  # noqa: F811
    imports: str,
    call: str,
    base_call: str = "",
) -> str | None:
    """The rule's reason when the base imports `imports` and the head's dep map also runs `call`.

    `base_call` runs in the dep map at both ends, before the head's `call`.
    """

    def with_lines(plugin: str, code: str) -> str:
        lines = "".join(f"    {line}\n" for line in code.split("\n") if line)
        return plugin.replace(_DEP_MAP_RETURN, lines + _DEP_MAP_RETURN)

    plugin = with_lines(f"{imports}\n\n\n{TOPOSORT}", base_call)
    tree.write("ansible/filter_plugins/toposort.py", plugin)
    base = tree.commit("toposort imports what the head calls")
    head = _commit(
        tree, [{"ansible/filter_plugins/toposort.py": with_lines(plugin, call)}]
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
        (
            "import logging",
            "logging.Logger.manager.loggerDict['order-only-5081'] = logging.PlaceHolder(logging.root)",
        ),
    ],
    ids=[
        "sys-path-append",
        "warnings-filters-insert",
        "deepcopy-dispatch-item",
        "item-three-levels-down",
    ],
)
def test_an_in_place_change_to_a_module_held_object_keeps_the_line(tree, imports, call):  # noqa: F811
    """An item added to a list or dict a module holds keeps the line (#5060).

    The attribute still names the same object, so comparing attribute identity sees nothing.
    In a deploy the plugin runs in the controller process, so the added item persists for
    every later task and role. `loggerDict` sits three levels below `logging.Logger`, so the
    walk must copy at least three levels to see it (#5081).
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
        ("import sys", "sys.setdlopenflags(sys.getdlopenflags() ^ 0x100)"),
        ("import gc", "gc.freeze()"),
        ("import codecs", "codecs.register(lambda name: None)"),
        ("import os", "os.register_at_fork(after_in_child=lambda: None)"),
    ],
    ids=[
        "umask",
        "signal-handler",
        "recursion-limit",
        "nice",
        "setsid",
        "dlopen-flags",
        "gc-freeze",
        "codecs-register",
        "register-at-fork",
    ],
)
def test_a_change_to_process_state_keeps_the_line(tree, imports, call):  # noqa: F811
    """Process state that no module attribute holds and no audit event reports keeps the line (#5060).

    The subprocess the runner uses discards the change when it exits. In a deploy the change
    persists in the controller process for every later task and role. A codec search function
    and an at-fork handler live in interpreter state that no module attribute holds (#5081).
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
        ("import readline", "readline.write_history_file({path})"),
    ],
    ids=["dbm-open", "shm-open", "readline-history"],
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
    wherever the path points. `readline.write_history_file` does raise `open`, but with mode
    `'w'` and flags 0, so the hook's flag test passes it (#5081).
    """
    path = repr(str(tmp_path / "escaped"))
    reason = _reason_for_head_call(tree, imports, call.format(path=path))
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        (
            "import _multiprocessing",
            "_multiprocessing.SemLock(1, 1, 1, '/order-only-5063', True)",
        ),
        (
            "import _multiprocessing",
            "try:\n    _multiprocessing.sem_unlink('/order-only-5063')\nexcept OSError:\n    pass",
        ),
        (
            "import _posixshmem",
            "try:\n    _posixshmem.shm_unlink('/order-only-5063')\nexcept OSError:\n    pass",
        ),
    ],
    ids=["sem-lock", "sem-unlink", "shm-unlink"],
)
def test_every_replaced_unaudited_call_keeps_the_line(tree, imports, call):  # noqa: F811
    """The runner's other replaced calls keep the line too, not only the two the rows above use.

    Each call leaves nothing under the temp tree, and the `except` swallows the error a
    missing name raises, so only the replacement ending the run keeps the line. A misspelt
    name in the runner's table would bind a new attribute and leave the real call live.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import logging", "logging.root.setLevel(1)"),
        ("import logging", "logging.disable(50)"),
        ("import signal", "signal.alarm(3600)"),
        ("import signal", "signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])"),
        ("import atexit", "atexit.register(print)"),
        ("import logging", "logging.root.addFilter(lambda record: False)"),
        (
            "import logging",
            "logging.getLogger('ansible.utils.display').disabled = True",
        ),
        (
            "import logging\nimport ansible.utils.display",
            "logging.getLogger('ansible').disabled = True",
        ),
    ],
    ids=[
        "logger-level",
        "logging-disable",
        "alarm",
        "signal-mask",
        "atexit",
        "root-logger-filter",
        "new-named-logger-disabled",
        "existing-named-logger-disabled",
    ],
)
def test_a_change_to_object_attributes_or_timers_keeps_the_line(tree, imports, call):  # noqa: F811
    """A write to an object a module holds, or to a timer or mask, keeps the line too.

    `logging.disable` writes an attribute of the manager the root logger holds, two levels
    below the module. A pending alarm would kill the controller mid-deploy, and a blocked
    SIGTERM would make it ignore a stop. `logging.Logger` precedes `root` in the `logging`
    namespace, so the walk must reach the root logger at its shallowest depth to see its filters.

    A named logger sits in `Logger.manager.loggerDict`, three levels below `logging.Logger`,
    and its attributes four levels down (#5085). Importing `ansible.utils.display`
    creates the `ansible` logger, and the runner preloads that import before it copies module
    state, so that row's logger exists at both ends before the plugin runs. A module attribute
    holds that logger too, so the row does not depend on the fourth level; the rows below do.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        (
            "import codecs",
            "codecs.register_error('order-only-5081', codecs.strict_errors)",
        ),
        ("import faulthandler\nimport signal", "faulthandler.register(signal.SIGUSR1)"),
        ("import faulthandler", "faulthandler.dump_traceback_later(3600)"),
    ],
    ids=["codecs-register-error", "faulthandler-register", "faulthandler-watchdog"],
)
def test_a_call_the_runner_counts_keeps_the_line(tree, imports, call):  # noqa: F811
    """A codec error handler or a faulthandler handler or watchdog keeps the line (#5081).

    No function reads these back, so the runner counts the calls instead. A watchdog left
    pending would dump every thread's traceback into a deploy an hour in.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        (
            "import logging\nlogging.getLogger('order-only-5085')",
            "logging.getLogger('order-only-5085').disabled = True",
        ),
        (
            "import logging\n_LOGGER = logging.getLogger('order-only-5085')",
            "_LOGGER.disabled = True",
        ),
        (
            "import logging\nlogging.getLogger('order-only-5085')",
            "logging.getLogger('order-only-5085').setLevel(50)",
        ),
        (
            "import logging\nlogging.getLogger('order-only-5085')\nlogging.getLogger('other')",
            "logging.getLogger('order-only-5085').parent = logging.getLogger('other')",
        ),
    ],
    ids=[
        "getlogger-disabled",
        "bound-logger-disabled",
        "getlogger-set-level",
        "getlogger-parent-replaced",
    ],
)
def test_a_write_to_a_logger_the_plugin_module_creates_keeps_the_line(
    tree,  # noqa: F811
    imports,
    call,
):
    """A write to a logger the base's module code already created keeps the line (#5085).

    Both ends create the logger when the plugin's module code runs, and the head writes to it
    when the chain runs. No module attribute the walk starts from holds the logger, so it sits
    four levels down, under `logging.Logger.manager.loggerDict`. The last row replaces the
    `parent` object, which the fallback comparison of a logger's `bool`, `int`, `str` and
    container-length attributes does not see, so only the fourth level of the walk catches it.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "call",
    [
        "logging.getLogger('order-only-5085').disabled = True",
        "logging.getLogger('order-only-5085').setLevel(50)",
    ],
    ids=["disabled", "level"],
)
def test_a_write_to_a_logger_both_ends_create_in_the_filter_call_keeps_the_line(
    tree,  # noqa: F811
    call,
):
    """A write only the head makes to a logger both ends create while the chain runs keeps the line (#5085).

    The logger is created after both walks, so neither copies it, and `loggerDict` gains the
    same key at both ends. Only the logger's own attributes tell the ends apart.
    """
    reason = _reason_for_head_call(
        tree, "import logging", call, base_call="logging.getLogger('order-only-5085')"
    )
    assert reason and "every deploy runs" in reason


def test_a_logger_both_ends_create_in_the_filter_call_and_leave_alone_is_order_only(
    tree,  # noqa: F811
):
    """The control for the row above: a logger created and left alone at both ends discharges."""
    reason = _reason_for_head_call(
        tree,
        "import logging",
        "logging.getLogger('order-only-5085')",
        base_call="logging.getLogger('order-only-5085')",
    )
    assert reason is None
