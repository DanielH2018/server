"""The order-only rows for state the module-state comparison does not see (#5060, #5063).

`test_digest_covers_order_only.py` holds the order-only rows, and these would have taken it
past the test-module line cap, so they extend it here. Each row's base end already imports what the head end uses. Loading
a module only the head loads already keeps the line, so that shared import keeps each row to
the one shape the issue names.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers_order_only_unseen_state.py
"""

import importlib.util

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


def _reason_for_head_module_code(tree, imports: str, code: str) -> str | None:  # noqa: F811
    """The rule's reason when both ends' module code runs `imports` and the head's also runs `code`."""
    tree.write("ansible/filter_plugins/toposort.py", f"{imports}\n\n\n{TOPOSORT}")
    base = tree.commit("toposort module code at the base")
    head = _commit(
        tree,
        [{"ansible/filter_plugins/toposort.py": f"{imports}\n{code}\n\n\n{TOPOSORT}"}],
    )
    return digest_covers.Classifier(tree.root).reason("authelia", base, head)


def test_a_write_the_module_code_makes_to_an_object_it_created_keeps_the_line(
    tree,  # noqa: F811
):
    """A logger the plugin's module code creates and, at the head only, disables keeps the line (#5107).

    Both walks run after the module code wrote the flag, so neither end sees it change within
    its own run. In a deploy the disabled logger persists in the controller for every later task.
    """
    reason = _reason_for_head_module_code(
        tree,
        "import logging\n_LOGGER = logging.getLogger('order-only-5107')",
        "_LOGGER.disabled = True",
    )
    assert reason and "every deploy runs" in reason


_SLOTTED = (
    "class _Slotted:\n    __slots__ = ('value',)\n\n\n"
    "json.order_only_5107 = _Slotted()\njson.order_only_5107.value = 0"
)


@pytest.mark.parametrize(
    "imports, call",
    [
        (
            "import json\njson.order_only_5107 = {'a': {'b': {'c': {'d': []}}}}",
            "json.order_only_5107['a']['b']['c']['d'].append(1)",
        ),
        (
            "import logging\nlogging.getLogger('order-only-5107')",
            "logging.getLogger('order-only-5107').addFilter(logging.Filter('x'))",
        ),
        (f"import json\n{_SLOTTED}", "json.order_only_5107.value = 1"),
        ("import decimal", "decimal.DefaultContext.prec = 5"),
        ("import random", "random.seed(5107)"),
        ("import threading", "threading.stack_size(1 << 20)"),
        ("import signal", "signal.siginterrupt(signal.SIGUSR1, True)"),
    ],
    ids=[
        "container-five-levels-down",
        "named-logger-filters-five-levels-down",
        "slots-attribute",
        "c-object-decimal-context",
        "c-object-random-state",
        "process-thread-stack-size",
        "process-siginterrupt",
    ],
)
def test_a_change_outside_the_walk_or_the_process_state_read_keeps_the_line(
    tree,  # noqa: F811
    imports,
    call,
):
    """A change the four-level walk, `shallow` and `_process_state` all miss keeps the line (#5107).

    The first two rows change a container at the fifth level below a module attribute: a named
    logger sits at the fourth, so its `filters` list sits at the fifth. A `__slots__` value
    and a C object's state have no `__dict__` for `shallow` to copy. The thread stack size and
    a signal's restart flag are process state the runner neither reads nor counts. Each change
    persists in the controller for every later task and role.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


def test_a_file_created_through_a_module_the_runner_neither_replaces_nor_wraps_keeps_the_line(
    tree,  # noqa: F811
    tmp_path,
):
    """A Tcl interpreter's `open` creates a file and raises no audit event (#5107).

    `_tkinter` is in neither of the runner's `unaudited` or `counted` tables, and with an
    absolute path the file lands outside the temp tree the runner scans afterwards.
    """
    path = tmp_path / "escaped"
    reason = _reason_for_head_call(
        tree,
        "import _tkinter",
        "_tkinter.create(None, 'x', 'Tk', False, False, False, False, None)"
        f".eval('close [open {path} w]')",
    )
    assert reason and "every deploy runs" in reason


def test_an_item_replaced_five_levels_down_keeps_the_line(tree):  # noqa: F811
    """A same-length write five levels below a module attribute keeps the line (#5107).

    The list keeps its length, so comparing the module-created object between the ends sees
    nothing; only a walk that copies the fifth level compares its items within the run.
    """
    reason = _reason_for_head_call(
        tree,
        "import json\njson.order_only_5107 = {'a': {'b': {'c': {'d': [None]}}}}",
        "json.order_only_5107['a']['b']['c']['d'][0] = 1",
    )
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import readline", "readline.set_history_length(5201)"),
        ("import markupsafe", "markupsafe.escape('x')"),
    ],
    ids=["built-in-c-module", "shared-object-c-module"],
)
def test_a_c_module_outside_the_runners_list_keeps_the_line(tree, imports, call):  # noqa: F811
    """Importing a C module the real chain does not import keeps the line, at both ends (#5201).

    A C module holds state no walk or reader copies and can make calls that raise no audit
    event, as `readline`'s history length is and `_tkinter.create` does. `readline` is built
    into the interpreter, so the hook refuses it by name; `markupsafe._speedups` is a shared
    object.
    """
    spec = importlib.util.find_spec("markupsafe._speedups")
    assert spec and str(spec.origin).endswith(".so")
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


def test_a_c_module_created_through_imp_keeps_the_line(tree):  # noqa: F811
    """A C module created by a direct `_imp.create_builtin` call keeps the line (#5201).

    The call raises no `import` event, and `select` initialises in phases, so the module stays
    out of `sys.modules`, where neither the loader scan nor the second run's preload finds it.
    Only the runner's guard on the `_imp` call sees it. Both ends create the module, and only
    the head calls `epoll`, which raises no audit event.
    """
    reason = _reason_for_head_call(
        tree,
        "import _imp\nimport importlib.machinery",
        "_select.epoll().close()",
        base_call="_select = _imp.create_builtin(importlib.machinery.ModuleSpec("
        "'select', importlib.machinery.BuiltinImporter))\n_imp.exec_builtin(_select)",
    )
    assert reason and "every deploy runs" in reason


def test_an_open_event_with_a_writing_mode_string_keeps_the_line(tree, tmp_path):  # noqa: F811
    """The `open` event a C-level `fopen` raises, with mode `'w'` and flags 0, keeps the line (#5081).

    `readline.write_history_file` raises it that way, so the hook's flag test passes it and
    only the mode-string test ends the run. `readline` is outside the runner's `c_modules`, so
    the row raises the event itself and proves the mode-string test alone, not a C call.
    """
    path = repr(str(tmp_path / "escaped"))
    reason = _reason_for_head_call(
        tree, "import sys", f"sys.audit('open', {path}, 'w', 0)"
    )
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import logging", "logging._lock.acquire()"),
        ("import decimal", "decimal.getcontext().prec = 5"),
        ("import threading", "threading._counter()"),
    ],
    ids=["c-object-rlock-held", "c-object-thread-decimal-context", "c-object-count"],
)
def test_state_inside_a_c_object_the_runner_loads_keeps_the_line(tree, imports, call):  # noqa: F811
    """A change inside a C object of a module loaded before the hook keeps the line (#5209).

    An `RLock`, the thread's `decimal` context and an `itertools.count` are static C types
    the walk does not copy and no `readers` entry reads. A held logging lock deadlocks every
    other thread of the controller, and a narrowed context changes every later `Decimal`.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "imports, call",
    [
        ("import os", "os.set_inheritable(2, True)"),
        ("import gc", "gc.set_debug(gc.DEBUG_SAVEALL)"),
        ("import sys", "sys.set_coroutine_origin_tracking_depth(5)"),
    ],
    ids=["unaudited-fd-inheritable", "process-gc-debug", "process-coroutine-tracking"],
)
def test_an_unaudited_call_or_unread_process_state_keeps_the_line(tree, imports, call):  # noqa: F811
    """A call with no audit event to process state the runner neither reads nor counts (#5209).

    An inheritable stderr leaks into every child the controller spawns, and `DEBUG_SAVEALL`
    keeps every collected object alive for the rest of the deploy.
    """
    reason = _reason_for_head_call(tree, imports, call)
    assert reason and "every deploy runs" in reason


def test_a_reference_the_module_code_writes_to_an_object_it_created_keeps_the_line(
    tree,  # noqa: F811
):
    """A logger's `parent` the head's module code re-points keeps the line (#5209).

    The cross-end `signature` compares only scalars and container lengths, so a replaced
    object reference reads the same at both ends.
    """
    reason = _reason_for_head_module_code(
        tree,
        "import logging\n_LOGGER = logging.getLogger('order-only-5209')\n"
        "_OTHER = logging.getLogger('other-5209')",
        "_LOGGER.parent = _OTHER",
    )
    assert reason and "every deploy runs" in reason


@pytest.mark.parametrize(
    "base_call, call",
    [
        (
            "logging.getLogger('order-only-5209').addHandler(logging.NullHandler())",
            "logging.getLogger('order-only-5209').handlers[0].setLevel(50)",
        ),
        (
            "logging.getLogger('order-only-5209').addFilter(logging.Filter('a'))",
            "logging.getLogger('order-only-5209').filters[0].name = 'b'",
        ),
    ],
    ids=["handler-level", "filter-name"],
)
def test_a_write_to_a_handler_or_filter_of_a_logger_the_chain_creates_keeps_the_line(
    tree,  # noqa: F811
    base_call,
    call,
):
    """A write to the handler or filter of a logger both ends create in the chain (#5209).

    Neither walk copies a logger created while the chain runs, and its `signature` counts
    its handlers and filters without reading them.
    """
    reason = _reason_for_head_call(tree, "import logging", call, base_call=base_call)
    assert reason and "every deploy runs" in reason
